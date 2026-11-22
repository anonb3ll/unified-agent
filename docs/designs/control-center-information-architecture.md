# Control Center information architecture

> Superseded for navigation and panes by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479).

**Task:** #949 (first child of #947 — modern Unified-Agent UX roadmap).
**Author:** agent-claude, 2026-09-13.
**Verified against:** `main @ ad71086`; `packages/web/src/shell/{nav.ts,AppShell.tsx}`,
`packages/web/src/App.tsx`, representative panes (`tasks/`, `approvals/`,
`sessions/`, `attention/`, `evidence/`, `action-deck/`, `portfolio/`);
`docs/designs/wayfinder-pane-parity-audit.md`;
`conductor/tracks/web_ui_command_center_20260821/spec.md`.

This document is the product/IA foundation for #950 (design system), #951
(prototype), #952 (implementation), and #953 (dogfood). It is a decision
document, not a menu of options — where the shipped Wayfinder surface
(task #885, ~100% pane parity) already made a call, this document either
adopts it or explicitly overrides it with a stated reason. Task #948
("daily-use workflows"), referenced in #951's notes, does not exist in the
task queue as of this writing (`task_list` has no id 948) — treat this
document's "agent/workspace model" and "home surface" sections as the
substitute grounding for daily-use workflows until #948 is filed and can be
reconciled against it.

---

## 0. What already exists (do not re-litigate)

Wayfinder shipped a single-page app with:

- One flat top-level nav — `NAV` in `nav.ts`, 20 `NavId`s, rendered as one
  `role="radio"` group by `ModeSwitcher` inside `AppShell`. `App.tsx` is a
  20-armed `mode === ...` conditional with **no grouping, no nesting, no
  default "home"** (default mode is `"tasks"`).
- A persistent header: product wordmark, the nav, a live `AgentStatusBar`
  (polls `/api/agents` every 15s, always visible regardless of pane), a
  user badge, sign-out, theme toggle. This header chrome is the only thing
  that is currently "global" — everything else is siloed per-pane.
- 20 independent panes, each with its own TanStack Query hook, mostly SSE
  or poll-invalidated, each reading and rendering one backend concern in
  isolation. `AttentionPane` and `PortfolioMapPane` are the only two that
  already take an `onNavigate` callback and route into other panes —
  everything else is a dead end once you land on it.
- No command palette, no search surface, and no settings surface exist
  anywhere in `packages/web/src` today. These are not "IA gaps to
  reconcile" — they are net-new builds.

This IA keeps every one of the 20 shipped panes as the datasource/logic
layer. Nothing below asks for a rewrite of a pane's data hook or query
model. What changes is grouping, defaults, cross-linking, and three new
surfaces (Home, Settings, Search). Section 8 gives the exact per-module
verdict.

---

## 1. Navigation model

**Decision: replace the flat 20-item radiogroup with 7 top-level sections,
each containing 1–5 of the existing panes as sub-views.** The existing
panes are not renamed or re-implemented; `ModeSwitcher`/`nav.ts` gain a
grouping layer and `App.tsx`'s giant conditional is replaced by
per-section routing. This is the single biggest concrete IA move in this
document because the audit and the module list both show the same
problem from two angles: 20 flat destinations is already past the point
where a horizontal tab bar reads as a taxonomy instead of a wall of
labels, and several of those 20 destinations are different views onto the
*same* underlying object (a task, an agent, a run) with no link between
them.

| Section | Contains (existing `NavId` / module) | Why grouped together |
|---|---|---|
| **Home** | *(new — see §2)* | Landing surface; not a pane, a composition |
| **Work** | `tasks`, `board`, `dispatch`, `canvas` | Creating, claiming, and tracking units of work |
| **Agents** | `sessions`, `agent-topology`, `executor` | Who/what is running, and its structural map |
| **Activity** | `flow`, `telemetry`, `task-graph`, `langgraph`, `cycles` | Runs, timelines, execution history — one governance surface, five lenses |
| **Approvals** | `approvals` | Governance checkpoint (kept as its own top-level section — see §5) |
| **Evidence** | `evidence`, `memory` | The durable record: audit chain + shared notes |
| **Fleet & Health** | `devices`, `doctor`, `zulip` | Operational/integration status, not day-to-day work |
| *(header, not a section)* | Settings *(new)*, Search *(new, global)* | See §9, §10 |

**One existing nav item is deprecated, not relocated:** `actions`
(Action Deck). See §5 — its approval/task/workflow mutations fold into
row-level actions on Approvals and Tasks instead of a fourth place to
find the same pending approval.

**One existing nav item is repurposed, not kept as a peer destination:**
`portfolio` (Portfolio Map). Its own docstring calls it "hobby-builder
project cockpits" — a cross-project rollup (Unified Agent, Browser, Sun
Market, TAP), which maps onto the personal/professional/hobby portfolio
buckets already decided elsewhere in this workspace. That is a *scope
selector*, not a destination you go to and then leave — it belongs on
Home as the "which of my projects am I looking at" control, not as its
own top-level tab. See §2.

Sub-views within a section render as a secondary tab strip under the
section heading (same visual language `ModeSwitcher` already uses, one
level deeper) — not a new nav paradigm, just nesting the existing one.

**Why 7 and not fewer:** Approvals is pulled out as its own top-level
section instead of folded into Activity or Work specifically because
governance visibility must not depend on which section a person happens
to be browsing (see §5's mechanism). Six-or-fewer would have required
either burying Approvals inside another section or merging Agents into
Activity, which conflates "what is an agent, structurally" with "what
happened, chronologically" — two different questions people ask at
different times (see §3 and §4).

---

## 2. Home / dashboard surface

**Decision: Home is a composition, not a 21st pane.** It is built from
data three panes already fetch — `useAttention`, `useApprovals` (count
only), and `useAgents`/`useSessions` — plus the portfolio scope selector
repurposed from `PortfolioMapPane`. `App.tsx`'s default mode changes from
`"tasks"` to `"home"`.

**What Home shows, top to bottom:**

1. **Next action** — `AttentionPane`'s existing "next action" line and
   button, promoted to the top of Home verbatim (it already computes this
   via `nextActionForItems` and already routes via `onNavigate`). This is
   the single highest-priority line on the page.
2. **Attention table** — the existing `AttentionPane` table (severity,
   kind, title, reason, age), unchanged, just relocated from behind its
   own nav tab to living on Home. The `attention` `NavId` can be kept as a
   direct deep-link (bookmarkable, used by other panes' cross-links) even
   though Home is now the primary way most people see it.
3. **In-flight strip** — a compact row of currently-running
   sessions/dispatches (reuses `useSessions`/`useActivityStream`,
   filtered to open sessions), so "what's actually executing right now"
   doesn't require a trip to Agents → Sessions.
4. **Portfolio scope chips** — the repurposed Portfolio Map, rendered as
   filter chips (Unified Agent / Browser / Sun Market / TAP, matching
   `PORTFOLIO_PROJECTS`) that scope the attention table and in-flight
   strip to one project. Default: unscoped (all projects), matching
   current Portfolio behavior.

**What Home deliberately does not show:**

- Raw logs, full execution graphs, or LangGraph checkpoints — those are
  investigation tools, not situational-awareness tools; sending someone
  to Activity for them keeps Home fast to scan.
- The full task queue or kanban board — Home shows *what needs you*, not
  *everything that exists*. Work → Tasks/Board remains the "browse
  everything" surface.
- Settings, doctor/health detail, evidence chain detail — these are
  intentionally one click away (header/Fleet & Health), not competing for
  space on the page you land on 20 times a day.
- Anything requiring a mutation beyond the single "next action" button.
  Home is a triage surface; it hands you off to the section that owns
  the actual action (Approvals, Tasks, Dispatch) rather than growing its
  own parallel mutation surface — this is exactly the mistake Action Deck
  made (see §5) and Home should not repeat it.

---

## 3. Agent / workspace model

Today, Agent, Session, and Task are three unconnected flat panes
(`sessions`, part of `agent-topology`, `tasks`) with no shared object
identity in the IA — you cannot get from a task to "the session that's
working it" without knowing to guess which pane has that information.
This IA makes the relationship explicit as a first-class model, not just
a documentation note:

- **Agent** — a persistent identity (`agent-claude`, `agent-server-agent3`, …).
  Long-lived; exists independent of whether it is currently doing
  anything. Represented today by `AgentStatusBar` (header, always
  visible) and the agent rows inside `SessionsPane`.
- **Session** — a bounded, time-boxed instance of an agent doing work
  (a Claude Code session, a dispatched job). Has a lifecycle
  (open → closed), an activity stream, and *may* be tied to zero, one, or
  several tasks. Represented by `SessionsPane`/`useActivityStream`.
- **Task** — the durable unit of work (`task_list`/`TaskQueue`). Outlives
  any single session; can be re-claimed by a different agent/session
  after a stale claim. Represented by `tasks/TaskQueue` +
  `tasks/TaskDetail`.
- **Run** — one execution of a task by a session, producing the
  timeline/evidence trail (`flow`, `telemetry`, `langgraph` checkpoints,
  `evidence` records). A task can have many runs across its lifetime (a
  retry, a re-dispatch after a stale claim).

**Decision: Task Detail becomes the hub node.** `TaskDetail.tsx` already
renders id/title/status/project/claim-lock/review-flag as flat fields.
Add cross-links from Task Detail to:
- the claiming **Agent/Session** (query `SessionsPane`'s data filtered by
  `task_id`, since `AgentRow`/`SessionRow` already carry enough to filter
  client-side),
- its **Runs** (deep-link into Activity → Task Graph, which the audit
  confirms already supports `?task=` lineage filtering via
  `/api/execution-graph?task=`),
- its **Evidence** (deep-link into Evidence with `?task_id=`, which
  `EvidencePane.tsx`'s `readTaskIdParam()` *already implements* — this
  link only needs to be added on the Task Detail side, no Evidence work
  required),
- any **pending Approval** referencing it (filter `useApprovals` client
  side by task id, same pattern as Evidence).

This is why Section 8 calls most of this "wiring, not new panes" — the
backing data and even some of the query-param plumbing (Evidence) already
exists; what's missing is the cross-link affordance on Task Detail and
the equivalent affordance on Sessions/Approvals pointing back at the
task. Concretely this is new frontend surface work for #952, but it is
glue between existing modules, not a new module.

Board (`KanbanPane`) and Task Queue (`TaskQueue`) both already view the
same task object by status; that overlap is intentional and correct (a
board and a queue are legitimately different lenses on one list) and is
kept as-is under the Work section.

---

## 4. Task queue, runs/activity timeline, approvals, memory/evidence, integrations, health, settings, search

For each surface: purpose, primary object, relationship to neighbors, and
governance/checkpoint visibility.

### Task queue (Work → Tasks/Board)
- **Purpose:** browse, claim, and track every task; the "everything that
  exists" view Home deliberately excludes.
- **Object model:** Task (see §3), with Board as a status-grouped lens on
  the same rows as Queue.
- **Relationship:** Task Detail is the hub node (§3); Dispatch (Work →
  Dispatch) is how a new bounded job gets created against a task.
- **Governance:** claim locks and review flags are already surfaced as
  fields in `TaskDetail`; no change needed beyond keeping them visible at
  the same prominence when Task Detail gains cross-links.

### Runs / activity timeline (Activity)
- **Purpose:** "what happened and is happening" across execution —
  today split across `flow` (execution graph + metrics),
  `telemetry` (run waterfall), `task-graph` (lineage), `langgraph`
  (checkpoints), and `cycles` (autonomous-cycle cockpit).
- **Object model:** Run (§3), scoped by Task or by Agent/Session.
- **Decision:** these five stay five sub-tabs under one Activity section
  rather than being merged into a single mega-timeline. They answer
  different questions (execution shape vs. latency/exceptions vs.
  lineage vs. LangGraph-specific checkpoint state vs. autonomous-cycle
  budget/gate state) and forcing one visual timeline to carry all five
  would be a regression from today's specialized views. What changes is
  discoverability (grouped under one nav section instead of five
  scattered flat tabs) and cross-linking from Task Detail (§3), not the
  panes themselves.
- **Relationship:** Flow is the default sub-tab (it's the closest thing
  to a general-purpose "what's running" view); Task Graph is reached
  directly from Task Detail's run cross-link with the task filter
  pre-applied.
- **Governance:** none of these mutate anything — read-only by design
  already, kept that way.

### Approvals
- **Purpose:** the governance checkpoint — anything requiring an
  explicit human (or delegated-authority) sign-off before it proceeds.
- **Object model:** Approval, referencing a Task and/or an Agent action.
- **Relationship:** deep-linkable from Task Detail (§3) and from Home's
  attention table; the sole place approval mutations happen (§5 folds
  Action Deck's duplicate approval-mutation path in here).
- **Governance/checkpoint visibility mechanism (concrete, not a
  sentence):** a persistent **header attention badge** — a small pill
  next to `AgentStatusBar` in `AppShell`'s header, visible on every
  section, showing the live count from the same `useApprovals`/
  `useAttention` queries already polled for Home. Clicking it jumps to
  Home's attention table (or directly to Approvals if the count is
  approval-specific). This is the mechanism that keeps approvals "never
  buried" without making every section itself look like an admin
  console: one small persistent number in chrome that's already there,
  not a persistent full-width banner competing with page content. It
  costs one new component (`AttentionBadge.tsx`) reading data every other
  piece of this IA already fetches — no new backend route.

### Memory / evidence (Evidence section)
- **Purpose:** the durable record — cryptographic audit chain
  (`evidence`) and shared agent memory notes (`memory`), grouped because
  both answer "what was decided/recorded and can I trust it," as
  distinct from Activity's "what executed."
- **Object model:** Evidence record (chain-linked, tamper-checked) and
  Memory note (FTS-searchable, read-only in this pane already).
- **Relationship:** Evidence already accepts a `task_id` query param
  (`EvidencePane.tsx`); Task Detail should be the primary place that gets
  used from (§3).
- **Governance:** Evidence's tamper/chain-integrity badges are the
  existing mechanism and are correct as-is; this IA does not change how
  evidence integrity is surfaced, only how you arrive at it.

### Integrations / Fleet & Health
- **Purpose:** Devices (fleet hosts + doctor freshness), Doctor (capability
  health checks), Zulip (external coordination inbox, read-only per the
  Wayfinder audit's C-series notes — no send-from-UI without an approval
  gate). Grouped because none of the three is "my work" — they're
  "is the substrate healthy."
- **Object model:** Host/device, capability check, external message.
- **Relationship:** Doctor and Devices already overlap conceptually
  (per-host doctor snapshots); kept as two sub-tabs of one section rather
  than merged, since Devices' fleet-wide view and Doctor's
  capability-by-capability view serve different debugging moments.
- **Governance:** none of these mutate; Zulip staying read-only in the UI
  is preserved (see open question in §11 on whether that should ever
  change).

### Settings *(new)*
- **Purpose:** the one missing administrative surface. Today "settings"
  barely exists — only theme toggle and sign-out live in the header.
  Real settings needed: active capability/edition profile (`UA_EDITION`),
  visible audit-root scope (read-only display of what `security_audit`
  can see, per the cockpit's own audit-roots gotchas), agent token/identity
  display (which identity this session is authenticated as — `whoami`
  already fetched by `App.tsx`), and workspace/project defaults.
- **Object model:** Profile/session config, not a task-shaped object —
  this is why it lives off the primary nav (a gear icon in the header)
  rather than as an 8th top-level section: settings is configuration you
  visit rarely, not a place you monitor.
- **Governance:** settings is read-mostly in v1 — surfacing configuration
  that already exists in the backend (`profile_status`, `system_doctor`,
  `mcp_health_telemetry`) rather than adding new mutable settings, to
  avoid opening a new mutation surface ungated by this IA pass. Any
  settings mutation (e.g. changing edition/profile from the UI) is
  explicitly out of scope for #952 and should be a separate, reviewed
  decision — noted, not silently built.

### Search / command palette *(new)*
- **Purpose:** the fastest path to "go to X" and "do Y" without walking
  the nav tree — jump to a task by id/title, jump to an agent/session,
  jump to a section, or trigger a bounded quick action (open Dispatch
  pre-filled, open Approvals filtered to pending).
- **Object model:** a federated search over Task, Agent/Session, and nav
  destinations — not a new data source; it queries the same
  `task_list`/`useSessions`/nav-section data already loaded, client-side,
  for v1 (no new backend search endpoint required to ship something
  useful).
- **Relationship:** global, keyboard-invoked (`Cmd/Ctrl+K`), overlays
  every section rather than living inside one — this is the connective
  tissue that makes the Task→Agent→Run→Evidence relationships in §3
  fast to traverse without deep-linking every single pair manually.
- **Governance:** search is read/navigate-only in v1; it does not gain
  its own approval or mutation authority — actions it launches (Dispatch,
  Approvals) still go through those surfaces' existing gates.

---

## 5. Action Deck: deprecate as a standalone destination

`ActionDeckPane` (`actions` NavId) duplicates `Approvals` (its own
`useActionApproval` hook hits the same underlying approval-mutation path
`ApprovalsPane`'s `useApprovals` reads) plus adds ad hoc task-claim and
workflow-run forms. Today an operator has three places pending approvals
can be acted on inconsistently-surfaced: Approvals, Action Deck, and (via
cross-link) Attention. That is the opposite of "never buried" — it's
"buried under duplicates," which is worse for trust than under-surfacing.

**Decision:** deprecate `actions` as a top-level nav destination. Its three
capabilities move to where they belong as inline actions, not a fourth
pane:
- Approval action buttons → row-level actions in `ApprovalsPane`'s table
  (the canonical approvals surface, §4).
- Task claim/complete → row-level actions in `TaskDetail`/`TaskQueue`
  (Work section).
- Workflow run → a form under Work → Dispatch (workflows are bounded jobs,
  same conceptual bucket as a dispatch).

This is real #952 implementation work (moving mutation hooks, not just
moving a nav entry) but it removes a whole redundant pane rather than
adding one, which is a net simplification worth calling out as a change
to the shipped nav, per this task's explicit instruction to flag anywhere
current nav is wrong for daily use.

---

## 6. Reconciling with prior product-thinking (`web_ui_command_center_20260821`)

That spec's FR-1 ("one screen answers what is running, stuck, waiting on
The operator, changed, and who owns each lane") is this document's Home
surface, almost verbatim — this IA does not contradict it, it specifies
the surface that spec asked for but left unbuilt. FR-2 (dispatch +
observation loop, human-gated review/approval/merge) is preserved
unchanged: Dispatch stays a Work sub-view, Approvals stays the sole
mutation gate, nothing in this IA expands autonomous merge/deploy
authority. FR-3 (UX/reliability debt — CSRF, accessibility) is orthogonal
to IA and is out of this document's scope; #952 inherits the existing
C2/C4 gates from the Wayfinder audit unchanged.

---

## 7. Governance/human-checkpoint visibility — summary mechanism

Two concrete, already-buildable mechanisms carry all of this IA's
governance requirements, not five different vague promises:

1. **Header attention badge** (§4, Approvals) — persistent count of
   pending approvals + attention items, visible from every section,
   backed by data already fetched.
2. **Task Detail as hub node** (§3) — every governance-relevant object
   (approval, evidence record, run) a task touches is one click from that
   task's detail view, so "is this task's approval buried somewhere" is
   never a question — it's a link on the page you're already looking at.

Nothing in this IA relies on "make it prominent" as its own justification
— each governance surface names the mechanism above or an existing
one (Evidence's tamper badges, TaskDetail's claim-lock field).

---

## 8. Module-to-IA mapping (for #952)

| Existing module | New IA home | Verdict |
|---|---|---|
| `tasks` | Work → Tasks | maps cleanly |
| `kanban` | Work → Board | maps cleanly |
| `dispatch` | Work → Dispatch | maps cleanly |
| `canvas` | Work → Canvas | maps cleanly |
| `sessions` | Agents → Sessions | maps cleanly |
| `agent-topology` | Agents → Topology | maps cleanly |
| `executor` | Agents → Executor | maps cleanly |
| `flow` | Activity → Flow (default sub-tab) | maps cleanly |
| `telemetry` | Activity → Telemetry | maps cleanly |
| `task-graph` | Activity → Task Graph | maps cleanly |
| `langgraph` | Activity → LangGraph | maps cleanly |
| `cycles` | Activity → Cycles | maps cleanly |
| `approvals` | Approvals (top-level) | maps cleanly; gains row actions moved from Action Deck |
| `evidence` | Evidence → Evidence | maps cleanly; gains a cross-link target from Task Detail |
| `memory` | Evidence → Memory | maps cleanly |
| `devices` | Fleet & Health → Devices | maps cleanly |
| `doctor` | Fleet & Health → Doctor | maps cleanly |
| `zulip` | Fleet & Health → Zulip | maps cleanly |
| `attention` | Home (primary) + kept as deep-linkable `NavId` | maps cleanly, relocated |
| `action-deck` | **deprecated** — mutations redistributed to Approvals/Tasks/Dispatch (§5) | needs rework |
| `portfolio` | Home → scope chips (repurposed from standalone destination) | needs rework |
| *(none)* | **Home** | net-new (composition of existing data, no new backend) |
| *(none)* | **Settings** | net-new |
| *(none)* | **Search / command palette** | net-new |
| `auth`, `theme` | unchanged (infra, not nav destinations) | out of scope |

**Headline for #952:** of the 20 existing `NavId`s, **18 map cleanly**
onto the new grouping with no logic change (regrouping + optional
cross-links only), **2 need rework** (`action-deck` deprecated/redistributed,
`portfolio` repurposed into Home), and **3 net-new surfaces** are required
(Home, Settings, Search) — none of which need a new backend route; Home
and Search both compose already-fetched data, and Settings surfaces
already-existing backend state (`profile_status`, `system_doctor`,
`mcp_health_telemetry`) read-only in v1.

---

## 9. Open questions for the operator

Kept short deliberately — everything decidable without the operator's specific
risk/scope judgment was decided above, not parked here.

1. **Zulip send-from-UI.** The Wayfinder audit explicitly scoped Zulip to
   read-only pending an approval gate for sends. This IA keeps it
   read-only. Is that still right, or is there now an appetite for a
   gated send (e.g. routed through the same approval mechanism as
   Dispatch)? This is a security/scope call, not an IA call.
2. **Portfolio domain visibility.** The portfolio-buckets memory note
   flags an open classification-rule question between personal/
   professional/hobby buckets. This IA assumes Home's scope chips show
   all of Unified Agent/Browser/Sun Market/TAP by default (matching
   current `PortfolioMapPane` behavior) rather than defaulting to a
   "professional only" scope during work hours or similar. Confirm that
   default, since it's a personal-workflow preference only the operator can
   set.
3. **Settings mutability.** This IA scopes Settings to read-only in v1
   (surface existing config, no new mutation path) to avoid quietly
   expanding what the UI can change. If there's a specific setting
   (e.g. switching edition/profile from the UI instead of CLI/MCP) worth
   fast-tracking to writable, name it for #952 — otherwise it stays
   read-only by default.

---

## 10. Non-goals (deliberately out of scope for this IA)

- Redesigning any pane's internal data model, query hook, or backend
  route — this document is IA/grouping/cross-linking, not a rewrite.
- Expanding autonomous merge/deploy/approval authority beyond what's
  already gated (unchanged from the `web_ui_command_center_20260821`
  spec's non-functional requirements).
- Visual/design-system decisions (typography, color, density, component
  library) — that's #950's job entirely; this document only asserts
  structure, not look.
