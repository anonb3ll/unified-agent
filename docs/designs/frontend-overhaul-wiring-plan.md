# Frontend audit and wiring plan

> Superseded for navigation and panes by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479).

Task #1930 · agent-codex · 2026-10-07 · draft PR #1060.
Source inventory: `321d5306dae1b41cfbf41cb29e7656aabbd46cd4`.
This is a design and integration contract. The prototype has no live bindings.

## Audit scope and evidence

Reviewed the current shell, all 39 navigation destinations, Home, task detail,
memory browsing, communication, dispatch, organization controls, shared query
helpers, and registered HTTP routes. The machine-readable
[wiring map](frontend-overhaul-wiring-map.json) assigns every existing destination
one primary home, plus ten nested or missing surfaces. Listed HTTP method/path
pairs were checked against the route registrations using Python AST, without
importing the application or creating a local database. A registered route does
not establish that every desired field or mutation is supported.

The collaborative browser reached the live hub UI. Normal navigation displayed
Sign In; no credentials were retrieved or entered. The existing public
`?showcase=primitives` fixture rendered the real shell and controls without
authenticated domain records. At its measured 539 CSS-pixel viewport, the header
was approximately 322 pixels tall: brand/account controls, wrapped section tabs,
sub-view tabs, and agent presence precede the work. Authenticated pane findings
below are source observations; they are not a visual audit of the operator's live queue.
No private production records or screenshots are included in this PR.

Two misleading states were observed in that shell after protected reads failed
with HTTP 401: **“Nothing waiting”** and **“No agents registered.”** The source
confirms missing data defaults to zero/empty. These are failure-state defects,
not evidence that the queue is empty. An authenticated regression check remains
required when the live shell is changed.

## What changes in visual hierarchy

| Finding and evidence | Design action | Priority |
| --- | --- | --- |
| Two horizontal navigation levels, plus permanent agent strip; Fleet has ten peer tabs. `shell/AppShell.tsx`, `shell/ModeSwitcher.tsx`, `shell/nav.ts`; shell visually inspected. | Five primary sidebar destinations. Place resource exceptions on Overview; full roster under Organization. Use contextual views within each destination. | P1 |
| Outer pane title and border repeat inside pane headings and surfaces. `AppShell.tsx`, `memory/MemoryPane.tsx`, `tasks/TaskDetail.tsx`; repeated shell/container framing inspected in showcase. | One page title. Use quiet rows, whitespace and bounded inspectors; avoid a card enclosing another complete page. | P1 |
| “Nothing waiting” ignores loading/error/partial state; presence turns failed reads into “No agents registered.” `AttentionBadge.tsx`, `AgentStatusBar.tsx`; reproduced in showcase. | Loading, unavailable, partial and stale are explicit. Show a clear queue only after successful reads of its required sources. Preserve last known data with its age. | P0 |
| Runtime and infrastructure vocabulary competes with work: LangGraph, Platform, Credential Pool, Topology. `shell/nav.ts`. | Execution checkpoints under Runs; platform objects under Diagnostics; credential metadata under Resources. Search and old links still reach them. | P1 |
| Delivery receipts are under Fleet; messages under Agents; memory mixed with evidence in one seven-tab group. `shell/nav.ts`. | Receipts beside delivery; persistent Messages; Knowledge separate from Audit. Cross-link all three to the same work. | P1 |
| Home promotes high-priority count and capability catalog alongside action urgency. `home/HomeView.tsx`. | Lead with consequence, owner, next actor and age. Priority is metadata, not proof of a blocker. Capability discovery lives in Knowledge. | P1 |
| Task dependency editor and generic object links appear before much of the work context. `tasks/TaskDetail.tsx`. | Outcome, owner, blocking checkpoint and proof first. Dependency editing and raw object diagnostics in a contextual inspector. | P1 |
| Memory exposes IDs, title, tags, author and update time but not note content or retrieval explanation. Copy says FTS while backend supports hybrid retrieval. `memory/MemoryPane.tsx`, `tools/memory.py`. | Make title and useful excerpt primary; inspect content, source, freshness, supersession and why recalled. Move IDs and metadata into detail. | P1 |
| Lifecycle and configuration controls are useful but consequential. `org/AgentLifecycleControls.tsx`, `org/ConfigRevisionsSection.tsx`. | Keep under Authority / advanced inspection with current target and consequences. Preserve backend refusal and confirmation; no inline everyday terminate control. | P1 |
| Generic dark blue palette and occasional literal badge colors lack one product vocabulary. `index.css`, `shell/ModeSwitcher.tsx`. | Apply verified Mocha tokens: rose actions/selection, neutral dark surfaces, green confirmed, yellow waiting/unknown, blue links/metrics, teal monitoring context. Labels carry meaning too. | P2 |

The redesign retains useful existing behavior: skip link, keyboard navigation,
high-contrast mode, profile filtering, guarded writes, task cross-links, scoped
queries, honest unknown quota, message retry identity, and evidence inspection.
The prototype's light companion is illustrative; production high-contrast mode
must remain available and receive its own checks.

## Coverage and discoverability

The prototype's contextual view buttons and search expose the complete mapped
inventory. These are design previews, not implemented live panes. A secondary
view does not lose its deep link or disappear from search.

| Primary home | Contextual views | Promote to Overview when |
| --- | --- | --- |
| Delivery | Tasks/board, planning/goals/graphs/canvas, runs/cycles/checkpoints, releases/receipts, automation | Blocked outcome, missing owner, failed run, review wait, overdue commitment, missing proof |
| Organization | Teams/topology/startup, sessions/native continuity, authority/operating/config/lifecycle, resources/quota/hosts/cloud/credential metadata, diagnostics, identity | Capacity exhausted or unknown, stale health, policy conflict, abandoned work, intervention required |
| Knowledge | Memory, source vault, tools, skills/team definitions, learning proposals, connections | Relevant unresolved conflict, expired source, connector authorization prevents current work |
| Analytics | Delivery flow, run monitoring, costs, quality/SLO/scorecards | Material reliability or budget exception with a defined observation window |
| Decisions | Canonical approvals, organization changes; plan review cross-linked from Delivery | An authorized actor must decide before progress |
| Messages | Conversations, coordination connections, outbound delivery | Unanswered request, failed return, handoff awaiting acknowledgement |
| Audit | Records/ledger, lineage, verification and export inspection | Required proof absent, verification failed, attribution incomplete |

Dispatch stays in the top bar and Delivery. It uses a canonical task and creates
a run; it does not create a second work queue. Skills/team definitions cross-link
to Organization's team startup. Plans keep both planning and decision context.

## Wire contracts and gaps

Browser reads use authenticated, permission-aware HTTP adapters. Mutations use
the existing CSRF and policy gates, with actor attribution and stable retry IDs
where supported. Reorganizing a screen cannot broaden a profile's reach. Reuse
existing adapters in `hub.ts`, `messages/api.ts`, `operating/api.ts`,
`skills-teams/api.ts`, `teams/api.ts` and the inline Org adapters before adding
new ones. The JSON inventory lists existing routes; the gaps below identify
where a backend contract is still needed.

| ID / priority | Surface and existing wire | Missing work / acceptance |
| --- | --- | --- |
| G1 / P0 | Attention and presence: `GET /api/attention`, `/api/approvals`, `/api/agents`. | Distinguish successful empty, loading, denied, unavailable, stale and partial. Dedupe overlapping task/approval attention by stable domain identity. Simulate a failed source; the UI must never claim a clear queue or no roster. |
| G2 / P1 | Canonical Delivery: tasks, why-chain, comments, plans, review-approval, executor-delegation, sessions, work graph, receipts; generic object readers where supported. | Join by task/run/delegation/approval/evidence IDs, never title. Paginated windows must disclose incomplete joins. Separate process completion, review, authorization, merge, deployment and verified behavior. A missing receipt stays unverified. |
| G3 / P1 | Receipt list: `GET /api/dispatch_receipts` supports task/state filters; frontend fetcher currently exposes only the global list. Full tool: `dispatch_delivery_receipt(run_id)`. Work products have separate submit/release routes. | Extend list adapter filters. Add a guarded run-detail HTTP adapter to the existing receipt tool if no supported object reader covers it. Include policy/approval outcomes, executor attribution, lease and transitions. `has_evidence` alone is not a verified release. Do not label a run receipt as deployed product proof. |
| G4 / P0 before live approval redesign | Decisions: approval list/detail and `POST /api/action/approval`. Existing body accepts `id`, `decision`, `reason`; the handler requires the appropriate human actor. | Prototype statement digest/preflight is target UX. No expected statement digest field was found in this HTTP contract. Define a server-enforced statement/revision/expiry precondition and presence policy before showing guaranteed matching-statement approval. Retain current refusal behavior; conversations and registered passkeys do not satisfy presence. |
| G5 / P0 before bounded-launch claims | Dispatch: routes, effective operating resolution, dispatch-stack preview, `POST /api/action/dispatch`; operating-run overrides use their own guarded action. | Current dispatch body supports agent/project/prompt/task/context/model, not the prototype's scope/time/spend/output fields. Inspect effective operating settings and enforcement metadata. Distinguish requested, recorded and enforced limits; only persist supported settings through their own contract. Budget admission is not a universal mid-run spend kill switch. |
| G6 / P1 | Messages: conversations/thread/sessions, inbox summary, `POST /api/messages`. Reads can include task identity. | Current send allowlist has act/body/message_id/recipient/reply_to/expects_reply, without task_id. Reuse reply linkage for existing threads; define a guarded task-binding contract for newly composed work conversations. Preserve stable message_id on retry, commit-order seq pagination, own/all visibility, delivery/read/answer distinction, and 202 approval parking. |
| G7 / P1 | Memory browse/search `/api/memory`; authorized `memory_note` object detail; existing `memory_get`, hybrid `memory_search`. | Frontend-only first step: expose content, author, source/evidence, validity, expiry, contradiction/supersession and retrieval scores/explanation when returned. Normalize raw tags safely. Full revision history and conflict resolution are separate gaps; do not infer numbered versions or overwrite another author. Source citation is not proof verification. |
| G8 / P1 | Tools: capability catalog; object-model manifest; skills/teams; connector lifecycle. | Catalog is curated capability evidence, not every tool's input schema. Define an authorized read-only registry metadata projection for tool names, parameters, limits, profile reach and source. No arbitrary JSON tool runner. Show registered separately from authorized, usable and currently observed healthy. |
| G9 / P1 | Analytics: flow-metrics, telemetry, costs, scorecards, delivery_slo, receipts. | Proposed 75% verified-delivery and wait cohorts are fictional; existing endpoints are not proof those metrics already exist. Define numerator/denominator, terminal states, interval, units, exclusions, timing coverage and evidence completeness. Separate estimated/known/unknown cost and subscription usage. Drill-through must use the exact cohort. |
| G10 / P1 | Audit: evidence, events, ledger rows, provenance graph and existing evidence pane. | Reuse verification results, not a client-inferred trustworthy badge. Distinguish chain integrity, attribution, authority, human presence and external anchors. Export needs a supported report contract carrying methods, exclusions, observation time and coverage; the prototype does not generate a real report. |
| G11 / P2 | Automation: existing MCP routine list/pause and HTTP routine fire. | HTTP route inventory has fire but no routine list/pause adapters. Reviewed workflow launch already uses `/api/action/workflow`; its catalogue currently comes from Overview. Give workflows and routines one Automation home while preserving their different contracts. Add authorized read/manage projections before presenting a complete schedule cockpit. Surface owner, next run, last outcome, effective limits and pause state. Preserve existing timer authority. |
| G12 / P1 | Shared refresh: existing calm polling/invalidation helpers and `/api/events/stream`. Existing Dispatch Studio uses composite Overview. | Preserve hidden-tab pause, 5-second coalescing, backoff, seq resume and stream-cap handling. Choose one coordinated stream/cache rather than mounting a subscription per sidebar destination. Focused query endpoints avoid repolling the broad Overview payload for unrelated panes. The legacy task-graph fetcher also slices `task_graph` from Overview; it needs a focused projection or explicit work-graph migration. Check active transport work ownership first. |

## Sequenced implementation

1. **Truth states and shell:** G1; theme tokens, sidebar, contextual discovery,
   legacy links and accessibility. No backend authority changes. Every legacy
   URL and alias resolves with relevant task/project parameters; unrelated
   parameters clear on navigation. Decide push/replace and browser back behavior
   explicitly. Active panes remain lazily mounted.
2. **Delivery and communication:** G2, G3, G6. One inspector binds task → run →
   review → decision → evidence → message → receipt. Demonstrate one successful
   delivery, failed execution, blocked release and missing-proof completion.
3. **Authority and launch:** G4, G5. Separate reviewed backend contracts from
   frontend bindings. A stale statement, denied actor, exhausted capacity and
   unsupported limit each produces an actionable refusal, with no implied launch.
4. **Knowledge and tools:** G7, G8. Start with fields already returned. Scope
   conflict editing, version history and full registry metadata separately.
5. **Analytics, audit and automation:** G9–G11; G12 throughout. Use explicit
   cohorts and observed verification. Retain unknowns and show freshness.

Before any live-code slice, refresh hub ownership and claims for overlapping
shell, transport, dispatch and approval work. Reconcile dependencies rather than
creating competing writers. Each slice uses an isolated worktree and existing
exact-head CI, genuine review, approval and steward shipping gates. This design
PR does not authorize production replacement or skip those gates.

## Review checkpoints

- Default view answers what needs attention, why, owner, age and next actor.
- All 39 destinations and nested features stay discoverable without equal
  prominence; current links and aliases continue to work.
- Loading, empty, denied, error, partial and stale states are independently
  exercised. A read failure never becomes a reassuring zero.
- Keyboard, touch and 320–1024 pixel reflow are checked; preserve high contrast
  and test focus restoration, drawer/dialog access and browser back navigation.
- Live metric values and enforcement statements require their own field-level
  evidence. Fictional prototype values never enter a production query cache.

## Palette provenance

Read-only verification found matching author and generated palettes at
`~/.config/linux-desktop/themes/operator-mocha/colors.toml` and
`~/.local/state/linux-desktop/current/theme/colors.toml`. Dark background `#11111b`,
panel `#181825`, selection `#45475a`, text `#cdd6f4`, readable secondary text
`#bac2de`, rose `#f38ba8`, green `#a6e3a1`, yellow `#f9e2af`, blue `#89b4fa`
and teal `#94e2d5` are used in the prototype. The profile's dark muted gray is
not used for body text. No desktop theme or generated files were changed.
