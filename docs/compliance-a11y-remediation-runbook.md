# Compliance wedge: accessibility remediation with signed evidence

Task #1131 (Front 1.d) — operator runbook for the regulated-services buyer
story. This slice reuses the **shipped** approval queue, producer HMAC signing,
`evidence_report`, and `compute_attribution_coverage`. It does not register a
new MCP tool or connector; `a11y_remediate` is only an approval-queue label.

## What you are proving

| Step | Platform surface |
| --- | --- |
| Finding recorded as a task | `task_add` / task notes |
| Agent proposes a fix | remediation payload with `task_id` |
| Human decides | `check_approval_gate` → `approval_resolve` |
| Signed accountability | `agent.evidence.producer_authenticated` (HMAC-SHA256-v1) |
| Buyer-readable proof | `evidence_report` + attribution coverage |

Five-link chain: **human → device → agent → action → proof**.

## Prerequisites

- Repo checkout with `uv sync` completed.
- No hub required — the demo uses an isolated `UA_HOME` (does not touch the
  live Mac hub `memory.db`).
- Demo bearer is a **local fixture string** in process env only
  (`demo-compliance-bearer-task-1131-not-a-secret`). Do not copy real
  `UA_AGENT_TOKENS` into the repo or transcripts.

## Approve path (happy path)

```bash
cd /path/to/unified_agent   # or this worktree
uv run python scripts/demo/a11y_compliance_remediation.py \
  --decision approve \
  --out /tmp/ua-a11y-approve \
  --print-json
```

Operator steps the script encodes:

1. Seed a WCAG 1.1.1 finding (image missing `alt`) into a fixture HTML file.
2. Create a remediation task claimed by the agent (`agent-cursor`).
3. Queue `a11y_remediate` via the real approval gate (`UA_APPROVAL_TOOLS`).
4. Resolve as the verified operator (`Desktop_Person`) with
   `approval_resolve(..., "approve")` — stamps the signed producer event.
5. Retry the gated call (consumes the one-shot pass) and apply the alt-text fix.
6. Mark the task `done` with an `evidence_ref`.
7. Emit JSON + Markdown evidence artifacts under
   `/tmp/ua-a11y-approve/artifacts/approve/`.

Expect:

- `remediation_applied: true`
- `provenance_verified: true` / `actor_authentication_verified: true`
- Fixture contains `alt="Company logo"`
- Artifact HMAC field present; **no bearer token** in the report JSON

## Deny path (fail-closed negative case)

```bash
uv run python scripts/demo/a11y_compliance_remediation.py \
  --decision deny \
  --out /tmp/ua-a11y-deny \
  --print-json
```

Same queue through step 3, then:

4. Operator runs `approval_resolve(..., "deny", reason=...)`.
5. Retry of the exact payload raises `DENIED` — remediation is **not** applied.
6. Task stays `blocked` with a denial note; evidence records the denied approval.

Expect:

- `remediation_applied: false`
- `denial_recorded: true`
- Fixture **unchanged** (still missing alt)
- Evidence `action.approvals[].status == "denied"`
- No remediation claim in notes / summary

## Both paths (recommended for a live transcript)

```bash
uv run python scripts/demo/a11y_compliance_remediation.py \
  --decision both \
  --out /tmp/ua-a11y-both \
  --print-json
```

Produces sibling isolated homes plus `transcript.json`. A checked-in live-run
transcript (and slim artifacts, no SQLite DBs) lives at:


## Reading the evidence report

```bash
# Against the isolated demo DB from an approve run:
UA_HOME=/tmp/ua-a11y-approve/ua_home \
  uv run unified-agent evidence --task <task_id> --markdown
```

Or open the emitted `*.md` / `*.json` under `artifacts/`.

## Automated coverage

```bash
uv run pytest tests/test_compliance_a11y_remediation_slice.py -q
uv run pytest tests/test_real_release_wedge.py -q
```

## Real-release variant (task #1140)

Dogfood the same five-link chain against a **real** `main` merge (tonight:
`a4a9f80` / PR #1165), not a synthetic WCAG fixture.

### Isolated attest path (reproducible signed report)

```bash
uv run python scripts/demo/real_release_wedge.py \
  --decision both \
  --sha a4a9f801135c382846ece0ff88d1a0137f1f7641 \
  --out docs/samples/task-1140-real-release \
  --print-json
```

Operator steps the script encodes:

1. Resolve the release SHA subject/date from git (fail closed if unknown).
2. Create a claimed attestation task with `evidence_ref=<sha>`.
3. Queue `release_attest` via the real approval gate (`UA_APPROVAL_TOOLS`).
4. Resolve as the verified operator — stamps `agent.evidence.producer_authenticated`
   (HMAC-SHA256-v1) with `task_id` on the claim.
5. On approve: consume the one-shot pass, write `release-attestation.json`, mark
   the task `done`. On deny: fail-closed (no artifact; task stays `blocked`).
6. Emit JSON + Markdown evidence under `artifacts/{approve,deny}/` and assert
   the #1139 attribution coverage gate is `pass` on the approve path.

Expect (approve): `attestation_applied: true`, `provenance_verified: true`,
`attribution_coverage.gate: pass`, artifact HMAC present, **no bearer token**
in the report JSON.

### Live Mac hub approval hop

The hub's live `UA_APPROVAL_TOOLS` gates `delegate` (and `gcloud_ops`). The
real-release wedge drives one **live** approval through that path:

1. Create an attestation child under task #1140 with `evidence_ref=<release sha>`.
2. Call `delegation_manage(create, …)` so `check_approval_gate("delegate", …)`
   parks a pending row (payload carries `parent_task_id`).
3. A chief-of-staff identity resolves via `approval_resolve(…, "approve")`
   — producer HMAC stamp (task #1155).
4. Retry the identical create to consume the one-shot pass; cancel the
   attest-only child when the hop is complete.
5. Verify with `event_log_verify` + `event_log_query(channels=["agent.evidence.producer_authenticated"])`
   and `producer_sign.verify_producer_signature` against current `UA_AGENT_TOKENS`.

Checked-in live transcript + evidence:


Evidence-report improvements landed with this slice so `parent_task_id`
approvals and their producer events bind to the parent task, and the MCP
`EvidenceReport` TypedDict keeps `attribution_coverage` / `clean_pass` (no
more silent strip on the hub tool surface).

## Boundaries (do not expand in this slice)

- Do not change approval semantics or add a real `a11y_remediate` /
  `release_attest` MCP tool.
- Do not wire a speculative accessibility-QA connector.
- Do not commit real bearer tokens or production secrets.
- The isolated demo still uses a disposable `UA_HOME`; the live hop is the
  Mac hub approval + HMAC verification only.
