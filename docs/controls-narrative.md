# Controls Narrative: Evidence Ledger vs. External Frameworks

Task #1281 (Epic H5). This page maps **shipped** unified-agent platform
features — the "Approval Ledger" product surface that proves which human is
accountable for what an AI agent did — to named controls in three external
frameworks: the EU AI Act (Regulation (EU) 2024/1689), ISO/IEC 42001:2023,
and the AICPA SOC 2 Trust Services Criteria.

**Date checked: 2026-09-22** for the framework mappings below (AI Act, ISO
42001, SOC 2); **the "Gaps and roadmap" section was re-verified 2026-09-23**
(task #1280) against current `main` source. Regulatory and standards text
changes; re-verify before citing this page in a customer-facing claim.

## Scope and what "supports" means

Every row below names one thing this repo actually ships today, on
`origin/main` at the commit this page was written against, verified by
grepping the source and test files cited. Nothing here is a compliance claim.
Framework language like "conformity," "certified," or "complies with" is never
used for this platform. Instead:

- **"Supports"** / **"provides evidence toward"** — the shipped feature
  produces an artifact (a log, a signed report, a verifiable chain) that a
  compliance or audit function could use as partial evidence toward the named
  control. It does not mean the control is fully satisfied, and it does not
  mean unified-agent itself is certified against any of these frameworks.
- No row claims EU AI Act conformity, ISO 42001 certification, or a clean
  SOC 2 opinion. Those require organizational processes (risk management,
  a management system, an independent auditor's report) that sit outside
  what a codebase can ship.
- Full framework texts are paywalled (ISO 42001) or gated behind staggered
  legal application dates that changed after this Task was scoped (EU AI
  Act) — see the timeline below, verified against the primary text rather
  than assumed.

## AI Act application timeline (verified against EUR-Lex, 2026-09-22)

Regulation (EU) 2024/1689 (the "AI Act") entered into force on 1 August 2024,
20 days after its 12 July 2024 publication in the Official Journal, per its
own Article 113 staggered-application scheme:

| Date | What applies |
| --- | --- |
| 1 August 2024 | Entry into force (Regulation (EU) 2024/1689, OJ publication 12 July 2024) |
| 2 February 2025 | Chapter I (general provisions) and Chapter II (prohibited AI practices) |
| 2 August 2025 | Chapter V (general-purpose AI models), Chapter VII (governance), Chapter XII (penalties), and related provisions |
| 2 August 2026 (original) → **2 December 2027 (as amended)** | High-risk AI system obligations for **Annex III** standalone systems (Chapters III §§1–3 among others) — **postponed** by the "Digital Omnibus on AI," Regulation (EU) 2026/1744 |
| 2 August 2027 (original) → **2 August 2028 (as amended)** | High-risk AI system obligations for **Annex I** systems embedded in other regulated products (medical devices, machinery, vehicles, etc.) |

**The postponement is confirmed, not assumed.** Regulation (EU) 2026/1744 —
the "Digital Omnibus on AI," amending Regulation (EU) 2024/1689 (plus
(EU) 2018/1139 and (EU) 2023/1230) — was published in the Official Journal on
24 July 2026 and entered into force on 27 July 2026
([EUR-Lex, Regulation (EU) 2026/1744](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)).
It amends Article 113 of the AI Act to move the Annex III high-risk deadline
from 2 August 2026 to 2 December 2027, and the Annex I high-risk deadline from
2 August 2027 to 2 August 2028. It also pushes the AI-content-marking
(Article 50) deadline to 2 December 2026 and the national-regulatory-sandbox
deadline to 2 August 2027. **Article 50 transparency obligations and the
GPAI/governance/penalties provisions that already applied from 2 August 2025
are unaffected by the omnibus** — only the high-risk-system deadlines moved.

This page treats the amended dates as authoritative and flags every row below
that concerns Article 12/14/19/26 (all high-risk-system obligations) as
**not yet legally binding until 2 December 2027 (Annex III) / 2 August 2028
(Annex I)** — the shipped features below are ahead of the legal deadline, not
racing to catch it.

### Article texts consulted

- [Regulation (EU) 2024/1689 — EUR-Lex consolidated text](https://eur-lex.europa.eu/eli/reg/2024/1689/2026-07-27/eng)
- [Regulation (EU) 2024/1689 — original OJ text, EUR-Lex](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ%3AL_202401689)
- [Regulation (EU) 2026/1744 — "Digital Omnibus on AI," EUR-Lex](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)

Article summaries below are drawn from these EUR-Lex texts (cross-checked
against the European Commission's own AI Act Service Desk explainer,
[ai-act-service-desk.ec.europa.eu](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-113),
for the timeline only). Paragraph-level quotations are kept short and
attributed; where a fetch of the full consolidated EUR-Lex HTML was
incomplete, the summary is marked so.

## EU AI Act — control mapping

| Control | Shipped feature | Evidence artifact (this repo) | Gap |
| --- | --- | --- | --- |
| **Art. 12(1)–(2) — Record-keeping**: high-risk AI systems "shall technically allow for the automatic recording of events (logs) over the lifetime of the system," covering situations that may affect conformity and enabling post-market monitoring. | Hash-chained event log (`events.py`) that every task, approval, delegation, and evidence-producer action writes to, plus the `unified-agent-audit verify` / `unified-agent audit verify` CLI that recomputes and validates the chain. | `src/unified_agent/events.py` (chain writer), `src/unified_agent/audit/ledger_cli.py::run_verify_cli` (verify CLI, `unified-agent-audit verify` / `unified-agent audit verify`), tests: `tests/test_audit_ledger_chain.py` (`test_sequential_hash_chain_valid`, `test_tamper_payload_detected`, `test_tamper_prev_hash_detected`). | This is a hub-operator's own action log, not a conformity-assessment log scoped to a specific deployed high-risk AI system's Annex III use case — an organization would still need to map its own AI system's events into this log design. |
| **Art. 14(1),(4) — Human oversight**: systems "designed and developed... [to] be effectively overseen by natural persons," with measures letting an overseer "decide, in any particular situation, not to use the high-risk AI system... or otherwise disregard, override or reverse the output," and to "intervene... or interrupt the system through a 'stop' button." | Approval queue / policy gate: `UA_APPROVAL_TOOLS` parks a named tool call until a human resolves it via `approval_resolve`; policy YAML (e.g. `deploy/install-gate/policy.yaml`) declares which actions require this stop-and-confirm gate before execution. | `src/unified_agent/tools/approvals.py` (gate + `approval_resolve`), `src/unified_agent/config.py::approval_gated_tools` (`UA_APPROVAL_TOOLS` parsing), `deploy/install-gate/policy.yaml` (worked policy example), `.github/workflows/install-gate.yml` (CI proof: one gated action parked then resolved over HTTP). | Configurable tool gating supplements baseline and forced gates: agent `delegate` submissions are gated by default; verified human web submissions skip that gate. `UA_APPROVAL_BASELINE=0` removes the baseline, while `work_product_release` and production actions still force approval independently of `UA_APPROVAL_TOOLS`. Deployers must assess the complete configured policy and assign competent human oversight; these software primitives alone do not establish Art. 14 compliance. |
| **Art. 19(1) — Automatically generated logs**: providers "shall keep the logs... automatically generated by their high-risk AI systems, to the extent such logs are under their control," for an appropriate period, minimum six months absent stricter law. | Same hash-chained event log as Art. 12, persisted in the hub's durable SQLite store (`memory.db`) with nightly off-host snapshot backups (24 h RPO; continuous WAL replication was retired in task #1411), so logs under the operator's control survive host loss, not just accumulate. | `deploy/mac-hub/run-database-backup.sh` nightly snapshot (the task #1197 replication was retired in #1411), `system_doctor` `database_backup` + `wal_replication` checks, `scripts/db_restore_rehearsal.py` (promotability proof), `tests/test_litestream_assets.py`. | Retention is operator-controlled and not policy-enforced. `UA_EVENT_LOG_RETENTION_DAYS` defaults to **30 days** (`config.DEFAULT_EVENT_LOG_RETENTION_DAYS`), but it is applied only when `events.prune_events()` is invoked explicitly — nothing in the hub calls it automatically, so in practice logs persist until an operator prunes. An operator who schedules pruning at the default would fall *below* Art. 19's six-month floor; nothing in this repo prevents that. |
| **Art. 26(1),(5) — Deployer obligations**: use the system per the provider's instructions, "assign human oversight to natural persons who have the necessary competence, training and authority," and keep the automatically generated logs referred to in Art. 12 "for a period appropriate to the intended purpose... at least six months." | Agent token lifecycle (issuance, rotation, per-label identity) plus profile-based least-privilege (`profiles.py`) together scope *who* (which authenticated identity) can act with which authority, and the evidence report ties a resolved approval or completed delegation back to that identity when a signed producer event backs it. | `src/unified_agent/tools/token_lifecycle.py` (`agent_token_issue`, `agent_token_rotate`, `agent_token_list`, task #1198), `src/unified_agent/profiles.py` (`DEFAULT_PROFILES_BY_IDENTITY`, deny-list least-privilege overlays per session), `src/unified_agent/audit/evidence_report.py` (ties records back to `human_provenance`). | Least-privilege profiles are a deny-list over an allow-by-default surface today (`profiles.py` comment: "new tools remain reachable by default until they receive an explicit review") — not a default-deny posture. "Competence and training" of the assigned human overseer is outside anything software can attest to. |

## Honesty rules applied to the AI Act rows above

Per task #1217 / #1249 / #1250, the evidence ledger draws a hard line between
*actor authentication* (a valid cryptographic signature) and *human
accountability* (a verified human identity behind that signature):

- **An agent-bearer HMAC signature alone is never treated as human
  attestation** (task #1217). Every fleet agent label signs with the same
  `hmac-sha256-v1` scheme a human operator does; before task #1217 a uniquely
  verifying signature — agent or human — was promoted to
  `human_provenance.verified = true`. It now requires
  `human_authority == "credential-attributed"`, which only a *human*
  identity's credential can satisfy (task #1673 renamed the value from
  `verified`: a credential does not prove a present human).
  See `docs/evidence-report.md` ("Actor identity vs. human authority") and
  `tests/test_evidence_human_authority.py::test_signed_agent_identity_is_not_verified_human`.
- **IdP-verified human links exist only via WorkOS sessions** (task #1249).
  A producer event stamped under a verified `Authorization: WorkOS
  <sealed-session>` request (JWKS validation, exact issuer, one configured
  organization, live membership re-check) uses scheme `workos-idp-v1` and
  needs no static bearer for the identity — this is the only path that can
  set `human_authority: "credential-attributed"` for a human who is not also holding an
  `UA_AGENT_TOKENS` bearer. See `src/unified_agent/tools/token_lifecycle.py`
  callers and `tests/test_producer_idp.py::test_workos_session_approval_stamps_idp_producer_and_marks_verified_by_idp`.
- **Tailnet device identity is hub-resolved, never client-supplied** (task
  #1250). The `device` link in an evidence record only verifies when the
  request's peer resolves to a Tailscale node via the hub's own `tailscale
  whois` call — a client-asserted hostname is never promoted to
  `device_link.verified: true`. See `tests/test_tailnet_device.py::test_non_tailnet_peer_fails_closed_without_calling_whois`
  and `test_whois_failure_and_malformed_output_fail_closed`.

These three rules are why "signed" and "human-verified" are kept as distinct
claims throughout this page. A human link can be backed two ways: a
`workos-idp-v1` producer event (the human authenticated through the IdP —
reported as `verified_via: idp`), or an `hmac-sha256-v1` producer event
signed with a human identity's bearer token such as `Desktop_Person`
(reported as `verified_via: agent_bearer`). Only the first is an
identity-provider attestation; this page never presents the second as one.

## ISO/IEC 42001:2023 (AI management system)

ISO/IEC 42001's full clause text is paywalled — ISO does not publish the
standard's clause bodies for free, and this page does not invent or
paraphrase clause text it has not read. What is confirmed from ISO's own
public listing
([iso.org/standard/42001](https://www.iso.org/standard/42001) — note: this
page returned HTTP 403 to this session's automated fetch tool, so the
description below is drawn from the ISO page's title/abstract as surfaced in
search results, not a direct read of iso.org's page body) and from
widely-corroborated secondary summaries of the public abstract:

- Full title: **ISO/IEC 42001:2023 — Information technology — Artificial
  intelligence — Management system**.
- Published December 2023 by ISO/IEC JTC 1/SC 42. It is the first
  international standard specifying requirements for establishing,
  implementing, maintaining, and continually improving an **AI management
  system (AIMS)** — an organizational management-system standard (structured
  like ISO/IEC 27001, with a management-system core plus an Annex A control
  set), not a technical product standard.
- Because clause numbers are not confirmed from a source this session could
  read directly, **no clause numbers are cited below** — only the
  publicly-known area a shipped feature plausibly touches, marked
  "requires the standard text" where clause-level mapping would need the
  paywalled document.

| Control area (no clause number cited — see above) | Shipped feature | Evidence artifact | Gap |
| --- | --- | --- | --- |
| AIMS operational controls over an AI system's logging / traceability (typically an AIMS Annex-A-style control area analogous to ISO/IEC 27001's operational-security controls) | Same hash-chained evidence ledger as the AI Act Art. 12/19 rows above. | `src/unified_agent/events.py`, `src/unified_agent/audit/ledger_cli.py`, `tests/test_audit_ledger_chain.py`. | Clause-level mapping requires the standard text (paywalled) — this row names an area, not a clause. |
| AIMS human-oversight / accountability controls | Approval gate (`UA_APPROVAL_TOOLS`) + agent token lifecycle + least-privilege profiles, as in the Art. 14/26 rows above. | `src/unified_agent/tools/approvals.py`, `src/unified_agent/tools/token_lifecycle.py`, `src/unified_agent/profiles.py`. | Same: requires the standard text for clause-level mapping. An AIMS certification also requires an actual management-system program (risk register, management review, internal audit cadence) that no codebase can provide. |
| AIMS change-management / continual-improvement evidence | `CHANGELOG.md` `[Unreleased]`/versioned entries (assembled from per-PR `changelog.d/` fragments) plus the delivery-receipt and dispatch surfaces recording what ran and what the outcome was. | `CHANGELOG.md`, `src/unified_agent/tools/dispatch_report.py::dispatch_delivery_receipt`, `docs/tool-catalog.md` (delivery-receipt entry). | Same caveat as above; this is evidence a change happened and was recorded, not proof of an ISO-conformant change-management *process*. |

**Bottom line for ISO 42001:** this page can honestly say the ledger and
approval-gate primitives produce artifacts an AIMS auditor would plausibly
want to see (event logs, human-in-the-loop gates, change records) — it cannot
say which numbered clause they satisfy without reading the standard, and does
not claim to.

## AICPA SOC 2 Trust Services Criteria

Primary source consulted:
[AICPA & CIMA — 2017 Trust Services Criteria for Security, Availability,
Processing Integrity, Confidentiality, and Privacy (With Revised Points of
Focus — 2022)](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022).
This page confirms the document's exact title and that the "2017 TSC" is
still the operative criteria set, revised only for points of focus in 2022
(the underlying criteria were not changed). The document's five categories —
Security, Availability, Processing Integrity, Confidentiality, Privacy — are
confirmed directly from that AICPA landing page. The Security category's
internal breakdown into numbered Common Criteria (CC1–CC9, including CC6
"Logical and Physical Access Controls," CC7 "System Operations," and CC8
"Change Management") is standard SOC 2 audit vocabulary used industry-wide,
but the *points of focus* under each CC are inside the AICPA's downloadable
document body, which this session's fetch tool could not render past the
landing page — so, as with ISO 42001, no specific point-of-focus text is
quoted, only the CC-level mapping.

| Criterion | Shipped feature | Evidence artifact | Gap |
| --- | --- | --- | --- |
| **CC6 — Logical and physical access controls** | Agent bearer-token lifecycle (issue/rotate/list, per-label scoping) plus least-privilege profiles that bound which tools an authenticated session can reach. | `src/unified_agent/tools/token_lifecycle.py` (task #1198), `src/unified_agent/profiles.py`, `tests/test_profiles.py`, `tests/test_tool_classification.py`. | Deny-list, not default-deny (see AI Act Art. 26 gap above) — a CC6 auditor would flag the allow-by-default posture for unreviewed new tools. No physical-access control is in scope of a software repo at all. |
| **CC7 — System operations** | `system_doctor` health/telemetry checks (including the `wal_replication` check), event-log-backed monitoring of task/approval/delegation lifecycle, and the install-gate CI workflow that exercises the full auth-enforced HTTP path after every merge that changes the installer or a dependency manifest, and by manual dispatch on a release tag. | `mcp_health_telemetry` / `system_doctor` tool family, `.github/workflows/install-gate.yml`, `src/unified_agent/events.py`. | No incident-response runbook or SLA-backed monitoring program is shipped in-repo — the primitives exist, the operational process around them is the deploying organization's responsibility. |
| **CC8 — Change management** | CHANGELOG discipline (per-PR `changelog.d/` fragments, assembled into `CHANGELOG.md` at release), a CI gate (`ruff check`, `ruff format --check` and pytest on GitHub Actions: cheap gates on each pull request, affected tests on the merge queue, and the full suite on each merge-batch dispatch and the nightly schedule; see `docs/ci-batch-verify.md`), independent PR review (automated reviewers plus a named agent or human), and the local pre-push gate `scripts/local-gate.sh`. Conventional Commits are validated only for commits made through the `git_commit_staged` tool — not repository-wide (no hook or CI check enforces commit messages). | `CHANGELOG.md`, `.depot/workflows/ci.yml` (CI), `scripts/local-gate.sh`, `src/unified_agent/tools/git.py::git_commit_staged`. | Change management here is a software-engineering practice (tests, lint, review), not a formally documented change-management *policy* artifact a SOC 2 auditor would separately expect to see (e.g., a written change-management policy document, a CAB, documented rollback plans per change). |

## Gaps and roadmap (explicitly not claimed as shipped)

**Updated 2026-09-23** (task #1280): two items below that were pending on
2026-09-22 have since shipped to `main` — F2 (external anchor) and the
provenance graph. Both are re-verified against current source, not carried
over from the prior pass. F4 remains pending; it is unchanged.

### Shipped since the 2026-09-22 pass

- **F2 — external anchor (task #1265, PR #707, `93b95d23`).** The evidence
  chain can now be witnessed outside the hub's own database: an
  `AnchorBackend` (`src/unified_agent/audit/anchor.py`) anchors the current
  chain head to an RFC-3161 Time-Stamp Authority (`tsa`) or a signed commit
  in a separately configured git repository (`git`), in addition to the
  pre-existing vault-note default. `unified-agent audit re-anchor` records
  it; `unified-agent evidence verify` re-checks it, live and offline, on
  every report build. This closes the specific gap named in the prior
  version of this page: a party outside the operator's own database can now
  detect a wholesale chain replacement, not just an in-place edit — for the
  `tsa` backend, entirely independently, using the pinned TSA certificate and
  standard `openssl ts -verify` tooling. Evidence report schema moved to
  `1.4` to carry the anchor header field. Off by default
  (`UA_EVIDENCE_ANCHOR_BACKEND` defaults to the vault backend; `tsa`/`git`
  are opt-in). See [evidence-report.md](evidence-report.md#chain-head-anchoring-task-1265-epic-ff2)
  and `tests/test_evidence_anchor.py`.
- **Provenance graph (task #1287, PR #704, `5a1437ee`).** The flat per-record
  5-link chain is now also readable as a node/edge graph
  (`src/unified_agent/audit/provenance_graph.py`): human, device, agent,
  task, action, approval, and work-product nodes, a deterministic canonical
  hash, and two named queries (by human, by release/task-set) over
  `GET /api/platform/provenance-graph` and `unified-agent platform
  provenance-graph`. No MCP tool was added (the default tool-budget rule
  stayed intact). The evidence report gained a `provenance_graph` appendix
  (schema `1.3`). The Control Center shows the graph at
  `/ui/settings/audit/provenance`
  (`packages/web/src/app/settings/ProvenancePage.tsx`). See
  [provenance-graph.md](provenance-graph.md) and
  `tests/test_provenance_graph.py`.

### Still pending, not shipped controls

- **F4 — attestation block** (task #1267): no dedicated cryptographic
  attestation block (e.g., a signed statement binding a specific report to
  a specific point-in-time system state beyond the existing detached
  `evidence verify` signature and the F2 chain-head anchor above) exists in
  this repo yet. `src/unified_agent/audit/evidence_report.py` carries a
  comment marking where this lands (`# ... so task #1267 (F4 attestation
  block) can ...`); no attestation-block code exists behind it as of
  2026-09-23.
- **Work-product release (PR #701, task #1278)**: merged to `main` on
  2026-09-23 as `071431d`. Deployment to the hub requires a separate live
  verification; this source-control record does not establish it. The code
  adds a released document's
  chain (human → device → agent → model → document hash → approval) read
  back from the ledger by `unified-agent work-product evidence <id>`, and a
  separation-of-duties rule (`approvals.SEPARATION_OF_DUTIES_TOOLS`) under
  which the submitter can never approve its own release, and an anonymous
  approval is refused. Evidence: `docs/work-product-release.md`,
  `tests/test_work_product.py`. A pilot onboarding kit
  (`docs/pilot/`, task #1280) ships vertical policy-pack starting points, a
  reviewer guide, and an evidence-verification guide for this workflow. Off
  by default (`UA_WORK_PRODUCT_ENABLED`),
  so an Art. 14 claim for documents holds only where it is enabled.

## Sources

- [Regulation (EU) 2024/1689 (AI Act) — EUR-Lex, consolidated text as of 2026-07-27](https://eur-lex.europa.eu/eli/reg/2024/1689/2026-07-27/eng)
- [Regulation (EU) 2024/1689 (AI Act) — EUR-Lex, original OJ publication](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ%3AL_202401689)
- [Regulation (EU) 2026/1744 ("Digital Omnibus on AI") — EUR-Lex](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)
- [AI Act Service Desk — Article 113 explainer (European Commission)](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-113)
- [ISO/IEC 42001:2023 — AI management systems (ISO catalogue page)](https://www.iso.org/standard/42001)
- [AICPA & CIMA — 2017 Trust Services Criteria (With Revised Points of Focus — 2022)](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022)
- In-repo: [`docs/evidence-report.md`](evidence-report.md) (5-link chain, honesty
  rules, CLI/HTTP surface), [`CHANGELOG.md`](../CHANGELOG.md) `[Unreleased]`
  and `[0.3.0]` sections (Litestream replication, evidence-report signing).
- `gh pr view 701 --json state,mergeCommit` (run 2026-09-23: MERGED,
  `071431d`) for the work-product-release status above.
- `git log --oneline origin/main` (checked 2026-09-23: `93b95d23` PR #707/task
  #1265, `5a1437ee` PR #704/task #1287, both ancestors of the commit this page
  was updated against) for the F2 anchor and provenance-graph shipped status
  above, plus a direct read of `src/unified_agent/audit/anchor.py`,
  `src/unified_agent/audit/provenance_graph.py`, and their test files.
