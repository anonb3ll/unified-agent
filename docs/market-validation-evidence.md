# Public market-validation evidence

Status: **proposal for review before publication**

This document defines how the public Unified-Agent and capture/recall slices
record evidence of external demand. It is a measurement contract, not a claim
that either slice has users, traction, or product-market fit.

## Purpose and boundaries

Use this contract before publishing either repository so the later decision is
based on recorded evidence rather than changing the story after seeing results.

- Track each repository separately. A combined integration demo is additional
  evidence; it never substitutes for a standalone workflow in either repo.
- Collect evidence from explicit issue reports, workflow or recall reports, and
  direct conversations. There is no silent phone-home telemetry.
- Keep the source reference and the minimum context needed to verify the event.
  Do not copy private prompts, page contents, credentials, contact details, or
  full conversation transcripts into an evidence record.
- Treat every number below as a decision gate. A gate is not an achievement
  until the required evidence exists and the operator reviews the result.

This document does not authorize public repository creation, external
publication, hosted service work, autonomous collection, or a change to either
product's privacy boundary.

## Evidence vocabulary

Each event has exactly one primary `event_type`:

| Event type | Countable evidence | Minimum proof |
|---|---|---|
| `install` | An outside developer installs the named repository slice. | Version or commit, platform, and a report reference. |
| `activation` | The developer reaches the first usable state after installation. | The activation action and whether it completed. |
| `completed_workflow` | A relevant workflow completes successfully. | Workflow name, outcome, and report reference. |
| `repeat_use` | The same outside developer completes another relevant workflow within seven days. | An opaque participant reference and dates for both events. |
| `human_review` | A person participates in a review or handoff rather than an agent-only run. | Review role and outcome; no private review content. |
| `failure_recovery` | A user encounters a failure and completes a documented recovery path. | Failure class, recovery action, and outcome. |
| `feature_request` | A person states a concrete use case or requested improvement. | The request and its stated use case, scrubbed of private data. |
| `design_partner_interest` | A potential design partner asks to evaluate or shape the slice. | The request, role or organization category, and next step. |
| `willingness_to_pay` | A person or organization expresses a concrete paid-support or product interest. | The stated offer or next conversation, without recording payment data. |

`activation` is evidence of reaching a usable state, not evidence of durable
use. `repeat_use` is the retention signal for this contract and requires a
completed workflow on both observations.

## Record schema

Store one record per event, per repository. The following fields are required
unless marked optional:

| Field | Type and allowed values | Rule |
|---|---|---|
| `schema_version` | string, currently `1.0` | Bump for incompatible field or meaning changes. |
| `event_id` | opaque string | Must not contain a person's name, email, URL query, or prompt text. |
| `repo` | `unified-agent` or `capture-recall` | Never merge the two repositories into one count. |
| `event_type` | One value from the vocabulary above | Exactly one primary type per record. |
| `observed_at` | Date or timestamp | Use the least precise value that supports the gate. Do not infer a date. |
| `source_kind` | `issue`, `workflow_report`, `recall_report`, `direct_conversation`, or `design_partner_note` | Must identify an explicit human-provided source. |
| `source_ref` | Opaque local reference or issue URL | Link to the evidence location without copying its private contents. |
| `participant_ref` | Opaque stable reference, optional for non-person events | Use the same reference to establish repeat use; never store identity data here. |
| `workflow` | Short enum or string, optional | Use `handoff`, `review`, `recall`, or a documented workflow name. |
| `outcome` | `success`, `partial`, `blocked`, `wrong_result`, `nothing`, or `recovered` | Use the narrowest honest outcome. |
| `reported_at` | Date or timestamp | When the evidence was recorded, not when it is being summarized. |
| `notes` | Short scrubbed text, optional | State only context needed to interpret the event; no raw private data. |

The following fields are deliberately absent: star count, page views,
impressions, inferred user identity, raw telemetry, full prompts, page bodies,
credentials, payment details, and sentiment scores without a stated use case.

### Synthetic fixture

This is a schema example only. It is not a real event and must not be counted:

```yaml
schema_version: "1.0"
event_id: "synthetic-example-001"
repo: "capture-recall"
event_type: "completed_workflow"
observed_at: "YYYY-MM-DD"
source_kind: "recall_report"
source_ref: "issue:REPLACE_ME"
participant_ref: "participant:REPLACE_ME"
workflow: "recall"
outcome: "success"
reported_at: "YYYY-MM-DD"
notes: "Synthetic fixture; replace every placeholder before use."
```

The `recall_report` value belongs in `source_kind`, not `event_type`. For stored
records, use the primary event type `completed_workflow` or `repeat_use`; the
source kind identifies that the report came from a recall form.

## Collection and privacy rules

1. The user submits an issue or report voluntarily, or the operator records a
   direct conversation in a separately governed note.
2. The recorder scrubs private data and assigns an opaque `event_id` and, when
   needed, an opaque `participant_ref`.
3. The recorder validates the repository, event type, dates, source, and
   outcome before adding the event to that repository's evidence ledger.
4. A reviewer checks that the event is not synthetic, duplicated, self-reported
   by the team, or counted for the wrong repository.
5. A decision summary cites event references and limitations. It does not
   publish raw reports or identify people without separate consent.

Team members, agents, maintainers, and internal dogfood runs may expose bugs or
validate a workflow, but they do not count toward the outside-developer gates.
Synthetic fixtures are test inputs, never evidence. A combined integration
run must carry separate references for the capture/recall event and the
governed agent event.

## Decision gates

Evaluate each repository independently:

| Gate | Threshold | Interpretation |
|---|---:|---|
| Outside installation | 10 outside developers install | Tests whether the release can be adopted beyond the team. |
| Relevant workflow | 5 outside developers complete the core workflow | Use handoff/review for Unified-Agent and recall for the capture/recall slice. |
| Repeat use | 3 outside developers repeat within seven days | Each repeat must reference a completed workflow, not a page view or sign-up. |
| Human participation | Present in the evidence set | At least one person participates in a review or handoff; record the role and outcome. |
| Meaningful requests | Present in the evidence set | Requests include a concrete use case; vague praise does not qualify. |

These are decision gates, not forecasts or achievements. Run each gate for
12–24 hours when possible. Retire a gate that runs beyond 48 hours and decide
from the evidence gathered so far; do not extend the window to move a threshold.

## Shipped evidence view (Lane B, UA #731)

[`lane_b_telemetry.py`](../src/unified_agent/lane_b_telemetry.py) is a
read-only aggregator over this contract. It does not change the gates, create
tables, or add a web UI route. A missing source is reported as
`status: unavailable` with `count: null` — never as a silent zero.

```bash
$ uv run python -m unified_agent.lane_b_telemetry
$ uv run python -m unified_agent.lane_b_telemetry --vault "$HOME/vaults/main"
```

Default repos are `workroom` and `sample_app_b` (`anonb3ll/<slug>`). The report
shape is pinned by `tests/test_lane_b_telemetry.py`:

| Gate key | Target | Source | What is counted |
|---|---:|---|---|
| `outside_installs` | 10 | GitHub `GET /repos/{owner}/{repo}/traffic/clones` | `uniques`, not raw clone `count` |
| `completed_workflows` | 5 | hub memory + task notes | memory tags `lane-b-evidence` + `repo:<slug>` + `event:completed_workflow`; task notes matching `[lane-b-evidence repo=<slug> event=completed_workflow]` |
| `repeat_users` | 3 | vault `*.md` | frontmatter `lane_b_evidence: true`, `event_type: repeat_use`, unique `participant_ref` (anonymous notes still increment) |
| `direct_feedback` | 1 | vault + hub memory | vault `event_type` in `{feature_request, design_partner_interest}`; memory `event:feature_request` / `event:design_partner_interest` |

GitHub traffic needs `GITHUB_TOKEN` or `GH_TOKEN` and permission to read
clone stats. Vault notes skip hidden path segments. Totals for a gate become
unavailable if any repo's component of that gate is unavailable.

This view does **not** implement the proposal's `human_review` gate or the
`unified-agent` / `capture-recall` repo slugs. Those remain the decision
contract above; Lane B currently measures the two product slices in
`DEFAULT_REPOS`.

## Decision rubric

Write the outcome per repository, before reviewing the results, using the
following rubric. A result can recommend more than one path only when it names
the evidence that separates them.

| Evidence pattern | Recommended path | Required follow-up |
|---|---|---|
| The standalone workflow is useful, repeat use is present, and users request improvements but no paid need is established. | Open source only | Maintain the slice, publish limits, and keep collecting workflow evidence. |
| The standalone workflow is useful and people request help with setup, deployment, integration, or operations. | Paid support/deployment | Define a bounded support offer; do not imply a hosted product. |
| Multiple teams need shared authority, collaboration, or operational guarantees that local mode cannot provide, and they express concrete buying interest. | Hosted/team | Run a separate privacy, security, cost, and authority review before building. |
| A narrow user group repeatedly requests one specialized outcome that the general slice does not serve well. | Specialized product | Name the segment, workflow, and evidence that justify narrowing scope. |
| Gates are not met or evidence is contradictory, sparse, or only internal. | No decision / continue validation | Record the missing evidence and retire or revise the experiment; do not manufacture a positive result. |

Every decision summary must include: repository, observation window, counts by
event type, gate results, evidence references, exclusions, privacy limits, and
the chosen path. Stars may be reported as repository metadata elsewhere, but
they must not appear as a primary signal or decision input here.

## Publication checklist

Before publishing either repository:

- Attach this schema and the rubric to the release review.
- Provide a standalone report path for the core workflow. The current
  Sample_App_B recall report is an example of a user-submitted source; it does not
  authorize telemetry or merge the two repositories' ledgers.
- Confirm that the standalone demo works with the other repository absent.
- Prepare a synthetic fixture and a scrubbed example, clearly labeled as such.
- Obtain the operator's review for the evidence schema, decision gates, and any
  external GitHub publication or repository creation.
