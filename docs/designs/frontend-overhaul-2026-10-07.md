# Unified Agent frontend overhaul — first pass

> Superseded for navigation and panes by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479).

Status: proposal and interactive prototype, 2026-10-07. Task #1930.
Author: agent-codex. Inventory base: `origin/main` at `321d5306`.

The operator requested streamlining around audit, consistent delivery, organizational
control, monitoring, communication, advanced memory, unified tools, dispatch and
clear analytics. He selected an interactive prototype as the first deliverable.
The experience serves the same person operating one agent or an organization;
organization size changes the amount of detail, not the product identity.
The operator also requested a sidebar and a more modern feel: persistent desktop
navigation, restrained surfaces, clear active states, compact spacing and a
reflowed mobile navigation. The prototype now defaults to the verified the operator Mocha palette, with a light companion.
The [UI audit and wiring plan](frontend-overhaul-wiring-plan.md) records
visual findings, priority, concrete contracts and backend gaps; the
[wiring map](frontend-overhaul-wiring-map.json) covers all 39 current destinations
and ten additional nested surfaces.

## Product promise

Direct agent work, understand its progress, and prove how it was delivered.
The interface should continuously answer:

1. What needs my attention, why, and who acts next?
2. What outcome are we delivering, and where is it blocked?
3. Who owns the work, with what authority and capacity?
4. What does the organization know, and what supports that knowledge?
5. What changed, and can we verify the resulting evidence?

## Proposed navigation

Five primary destinations, plus four persistent controls:

| Destination | Primary question | Content |
| --- | --- | --- |
| Overview | What needs attention now? | Prioritized decisions, blockers, active delivery, freshness and health |
| Delivery | What are we trying to deliver? | Outcomes, tasks, plans, board, runs, reviews and release receipts |
| Organization | Who owns and controls the work? | Teams, people, agents, authority, operating profiles and resource health |
| Knowledge | What can the organization reuse? | Memory, sources, decisions, skills, tools and integrations |
| Analytics | How reliably are we delivering? | Lead time, review wait, failures, verified delivery and cost coverage |
| Decisions (persistent) | What requires an authorized decision? | Canonical approval queue, with scoped statement and preconditions |
| Messages (persistent) | Which conversation needs a response? | Internal conversations, session addressing, handoffs and delivery status |
| Audit (persistent) | What happened and what proves it? | Event history, lineage, integrity, exports and evidence gaps |
| Dispatch (persistent) | How do I launch bounded work? | Task, executor, scope, limits, preflight, authority and expected output |

Delivery has one canonical work detail. Runs, review, approval, memory citations,
messages and evidence link to that detail. Lists and graphs are alternative
views over those objects. Technical runtime names belong in diagnostics and
inspectors rather than primary navigation.

Knowledge separates recall from proof: memory is a useful claim with sources,
freshness and revision history; audit is the durable event record. Tools and
skills show availability, permissions, version and the work they support.
Registered, usable, unknown and unavailable must remain different states.

## Inventory and migration map

Current main defines 39 `NavId`s in `packages/web/src/shell/nav.ts`, with seven
section groups and a home dashboard. This is a current code inventory, not a
claim that every view is mature or every backend capability exists.

| Current views | Proposed home |
| --- | --- |
| home, attention | Overview |
| tasks, goals, board, plans, canvas, work-graph, task-graph | Delivery; graphs and canvas are alternate views |
| dispatch | Persistent Dispatch; also launchable from Delivery |
| sessions, org, operating, agent-topology, skills-teams | Organization; skills also cross-link from Knowledge |
| executor, devices, doctor, cloud, quota, credential_pool | Organization → Resources / diagnostics; Overview shows exceptions |
| messages, zulip, slack_gateway, notify_delivery | Persistent Messages; gateway health under Organization diagnostics |
| approvals | Persistent Decisions; one canonical resolution path |
| evidence, provenance | Persistent Audit and object-level evidence |
| memory, vault, proposals, capabilities, connectors | Knowledge; connections retain lifecycle / authority details |
| platform | Organization → technical diagnostics |
| flow, telemetry | Analytics |
| dispatch_receipts | Delivery → Releases; analytics cross-links to the same receipts |
| langgraph, cycles | Delivery → run diagnostics |

Retain existing deep links and aliases during migration. Unify navigation first,
then replace composite workflows incrementally. Do not rewrite query hooks,
SDK mirrors, authentication or authorization as a visual redesign side effect.
The prior IA document remains historical context; this is a proposal to revise it.
Native shell work is separate: shared frontend surfaces can serve browser and
native clients without introducing a second authority store.

## Interaction model

- **Overview:** a decision or failure explains its consequence, owner, age and
  next action. Counts link to the underlying records. Missing data is unknown.
- **Delivery:** outcome → task → run → checks → review → authority → deploy →
  receipt. Completion never means only a finished agent process or merged PR.
- **Organization:** inspect a team's assigned work, effective policy and capacity
  in one place. Role and permission edits keep their existing review and approval
  requirements. A one-agent workspace uses the same objects with fewer rows.
- **Messages:** keep request, acknowledgement, answer and handoff distinct.
  Messages do not confer authority. External send actions use supported gates.
- **Knowledge:** show sources, author, project, revision, freshness, conflicts,
  supersession and retrieval explanation. Unknown evidence is visible. Advanced
  memory functionality shown in the prototype is target UX, not a backend claim.
- **Dispatch:** select existing work and an executor, define allowed scope,
  budgets and expected output, inspect preflight, then use the existing launch
  gate. Show the resulting run and its policy receipt together.
- **Audit:** verification reports its coverage and exclusions. Chain integrity,
  actor attribution, human presence and external anchoring are separate facts.
- **Analytics:** define metric, interval, cohort, numerator, denominator,
  exclusions, freshness and coverage. A zero is never a substitute for unknown.

## Prototype

`unified-agent-overhaul-prototype.html` is an independent interactive UI fragment
with inline CSS and JavaScript. It uses fictional sample records and no hub
calls or writes. The optional decorative accent downloads a pinned static CDN
module only when motion is requested and WebGPU is exposed in a secure context.
It supports all destinations, work inspection with
linked tabs, memory/source inspection, conversation inspection, analytics
drill-through, local dispatch preflight and approval-readiness inspection.
Prototype IDs (`DEMO-*`) are unrelated to real task IDs.

Actions demonstrate a proposed workflow. They never approve, send, dispatch or
change a real record. The prototype does not establish live permission checks,
real metric calculation, production accessibility or live integration. The follow-up coverage map inventories every
current destination; coverage does not establish field-level support.

## Suggested delivery slices after UX review

1. **Shell and Overview.** Agree destination names; preserve all old links;
   implement persistent decisions/messages/audit/dispatch access and honest
   loading, partial, stale and unavailable states. Coordinate the existing
   `App.tsx`, `shell/nav.ts`, `hub.ts` and stylesheet ownership before coding.
2. **Canonical Delivery detail.** Connect existing task, run, plan, review,
   approval, conversation and receipt projections. Test a failure and a
   successful end-to-end delivery, including exact-head checks and deploy proof.
3. **Organization and communication.** Consolidate existing authority and
   resource views, then connect scoped conversations and handoffs. Editing or
   sending must use supported backend gates; no new authority from UI grouping.
4. **Knowledge.** Consolidate memory, vault, skills, capability and connector
   browsing. Define backend gaps for source provenance, conflict and freshness
   before promising advanced recall functions.
5. **Analytics and audit detail.** Build metrics from defined cohorts and
   existing evidence; expose coverage and verification gaps. Make every summary
   traceable to its underlying records.

Each slice should demonstrate a coherent operator workflow. Acceptance should
cover keyboard use, mobile reflow, empty/loading/error/partial states,
authorization refusal, stale preconditions and linked evidence. Ship each
through the existing exact-head review, CI, approval and steward process.
This proposal requests no production deployment or authority expansion.

## First-pass validation

The T3 headless HTML preview executed 67 browser assertions at each of 1024,
728, 360 and 320 CSS pixels. Checks covered every destination and its inspection
actions, work-detail tabs, tool browsing, decision preflight, task-dependent
dispatch scope, invalid time and whitespace scope, escaped user text, reply
validation, search filtering/empty state/keyboard shortcut and light/dark
switching. Every destination fit without horizontal overflow at those widths.
Desktop and phone previews were visually inspected; no JavaScript console
errors appeared. Validation was injected for the preview and is not part of the
prototype. These checks do not establish production keyboard or accessibility
conformance.

`git diff --check` and the scoped security audit passed. The live collaborative
preview connection dropped during testing, so interaction validation finished
in the supported T3 headless HTML preview. No full Python suite was run for this
standalone design artifact.

## Mocha and audit follow-up validation

The follow-up adds contextual discovery and search for the mapped inventory,
including reviewed workflows, routines, lifecycle/configuration, identity,
costs, quality, comments and diagnostics. All 39 legacy destinations and ten
additional surfaces have a planned home. Forty-seven contextual previews are
reachable; Overview itself and persistent Dispatch cover the remaining two.
These previews explicitly disclose that their live records/actions are not bound.

The browser executed 202 assertions (67 workflow checks plus 135 catalogue,
inspection and fit checks) at each of 1024, 728, 360 and 320 CSS pixels after the
final changes. No horizontal overflow or JavaScript console errors appeared.
The source map covers each legacy ID exactly once and every listed HTTP
method/path matches a registered route. Validation checks are transient preview
instrumentation, not a new product test suite. Live authenticated pane validation,
field-level contracts, production accessibility and shipping gates remain for
implementation. The audit separates rendered shell findings from source findings.

A focused dark-palette contrast check found rose text on the selection surface
below 4.5:1. Selected labels now use the foreground token, with rose emphasis
on the control border. Body, secondary, selected, primary-button and link
text pairs meet 4.5:1 in this check; this is not a full accessibility audit.

## Shader accent trial

The operator authorized trying [Shaders](https://github.com/shader-effects-inc/shaders)
in the prototype. One low-opacity `FlowingGradient` sits behind the Overview
introduction. It uses the verified profile's rose (`#f38ba8`), magenta (`#f5c2e7`)
and dark surfaces; the light companion resolves its own rendered tokens.
The layer is decorative, hidden from assistive technology and cannot intercept
pointer input. It carries no status or data meaning.

The prototype lazily imports the MIT-licensed engine's
[version-pinned ESM bundle](https://cdn.jsdelivr.net/npm/shaders@4.0.0/dist/js/bundle.js).
No package dependency or live frontend integration is added. Telemetry and
performance tracking are disabled. A CSS gradient is always available;
unsupported WebGPU, module/init failure and terminal device failure retain it.
Reduced-motion preference prevents initialization, including when changed
during use. The operator can pause motion; this choice survives navigation and
is included in optional host interaction state. Hidden tabs and offscreen
accents pause, while navigation, theme changes and page exit release the GPU
instance and observers. Epoch checks dispose late initialization results.

The trial does not establish production suitability. The general ESM bundle is
approximately 2.55 MB uncompressed, despite using one effect. Before production
adoption, verify the animated appearance on a real WebGPU adapter, measure load
and frame costs, and assess a smaller integration or a CSS-only treatment.
The live frontend, authorization and workflow contracts are unchanged.

### Shader trial validation

The final prototype passed 213 browser assertions at each of 1024, 728, 360 and
320 CSS pixels. These cover the existing workflows and inventory plus motion
controls, reduced motion, offscreen/tab pause, cleanup, late initialization,
recoverable errors and terminal failures. Shader lifecycle assertions use a
test double injected only into preview runs; they do not prove GPU rendering.
Separate previews passed unsupported-GPU, initial-reduced-motion and failed-module
fallback checks without downloading the shader module. Desktop and phone static
previews were visually inspected with no horizontal overflow.

The collaborative browser loaded the actual pinned module successfully, but
reported no available WebGPU adapter. The actual initialization therefore
returned to the static accent and disabled motion. The library's adapter
warnings were observed; animated appearance and GPU performance remain
unverified. This is an explicit validation gap, not a claim of working animation
on this preview host. No full Python suite was run for this design-only change.

## Primary-button highlight correction

Operator feedback exposed a CSS specificity error: the generic `button:hover`
rule replaced the rose primary fill with the dark secondary surface while
retaining the dark primary label. Primary-button selectors now preserve their
paired fill and text during hover. The existing subtle hover dimming and visible
keyboard focus ring remain. The correction applies to Review decision,
Dispatch work and the prototype's other primary actions.

The collaborative browser verified actual hover and focus states in dark and
light appearances. Filter-adjusted label contrast measured 7.24:1 in dark mode
and 7.53:1 in light mode. Desktop and phone layouts were checked again. These
focused appearance checks supplement the earlier workflow checks; no new
product test suite or full Python run was needed for this CSS-only correction.
