# UX Discovery: Workflow Mapping & Modern Product Benchmarks

**Task:** #948 (UX discovery: map current workflows and benchmark modern products)
**Parent Initiative:** #947 (Roadmap: modern Unified-Agent user experience)
**Author:** agent-agy, 2026-09-11
**Target Scope:** `docs/designs/ux-discovery-workflow-map.md`, informing #949–#953

---

## 1. Executive Summary & Objective

The **Unified Agent Control Center** has reached 100% Wayfinder pane parity across all 24 legacy surfaces (PR #484), completed accessibility hardening under WCAG 2.2 AA (PR #476), established cookie-based session recovery (PR #497), and wired live agent presence (PR #498).

However, the current user experience reflects its historical origins: an **administrative pane-switcher with 21 flat, horizontal navigation modes**. Operators (the operator and developer agents) must mentally map tasks to individual panes rather than engaging in seamless, guided workflows.

**Task #948** delivers an empirical UX audit and interaction benchmark. It maps the highest-value daily jobs-to-be-done, evaluates interaction patterns across leading modern software products (Spotify, Docker Desktop, HubSpot, Clay, Notion, Nooks, Gong, Salesforce, DoorDash, Moshi, T3 Code), and establishes a prioritized problem and opportunity map to guide subsequent architecture (#949), design systems (#950), prototyping (#951), and implementation (#952).

---

## 2. Current State Audit (Control Center As-Is)

### 2.1 Navigation & Layout Architecture
- **Header & Mode Switcher:** Displays a horizontal `radiogroup` with **21 distinct tabs** (`tasks`, `board`, `attention`, `sessions`, `approvals`, `dispatch`, `actions`, `flow`, `canvas`, `evidence`, `portfolio`, `telemetry`, `topology`, `task-graph`, `langgraph`, `zulip`, `doctor`, `devices`, `executor`, `memory`, `cycles`).
- **Cognitive Load:** High. When an operator opens the web UI, there is no primary visual focal point. A user looking to answer *"What needs my attention right now?"* must manually decide whether to look at `attention`, `approvals`, `tasks`, or `doctor`.
- **Screen Real Estate:** The horizontal tab list wraps across 3–4 lines on laptop screens, compressing the primary viewport before the user reaches active content.
- **Agent Presence Strip:** Renders a horizontal list of registered agents (`agent-claude`, `agent-agy`, `agent-codex`, `agent-server-agent5`, etc.) at the top of the page. While live data is now wired (PR #498), the pills are non-interactive: clicking an agent does not filter their tasks, reveal their active terminal/worktree, or open their session transcript.

### 2.2 Functional Panes Audit

| Surface / Pane | Primary Function | Current Limitations |
|---|---|---|
| **Attention** | Triage items needing human review | Unranked alert list; no direct resolution buttons; does not embed tool approval requests. |
| **Tasks & Board** | Master task queue & 5-column Kanban | Tasks mode has high vertical density but requires multiple clicks to inspect subtasks or claims; Board lacks quick keyboard actions. |
| **Approvals** | Gated tool action queue | Lists tool name and raw JSON payload; lacks surrounding task context, rationale, or blast radius visualization. |
| **Sessions & Activity** | Live agent event streams | Displays raw SSE event feeds; difficult to isolate a single agent's execution thread or view real-time token/step progression. |
| **Dispatch Studio & Actions** | Bounded job runner & action deck | Isolated forms; cannot prefill parameters from a selected task or attention item. |
| **Evidence & Memory** | SHA-256 audit ledger & note browser | Evidence is purely linear; Memory search is decoupled from task execution context. |
| **Diagnostics (Doctor, Devices, Telemetry)** | Capabilities, hosts, latency waterfall | Separate tabs for tightly coupled operational data; no unified "system pulse". |

---

## 3. Core Daily Workflow Mapping (Jobs to Be Done)

Through real-world daily operation by the operator and the agent fleet, six primary workflows define the core user journey:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                      SIX CORE DAILY WORKFLOW LOOPS                           │
├──────────────────────────────────────────────────────────────────────────────┤
│ 1. TRIAGE & BRIEFING   --> "What requires my decision right now?"            │
│ 2. FLEET COCKPIT       --> "What is each agent currently executing?"         │
│ 3. GOVERNED APPROVAL   --> "Inspect context, verify blast radius, 1-key auth"│
│ 4. TASK ORCHESTRATION  --> "Break down work, claim tasks, dispatch workers"  │
│ 5. AUDIT & MEMORY      --> "Review cryptographic proof, recall decisions"    │
│ 6. SYSTEM PULSE        --> "Verify hub, bridges, workers, and MCP gates"     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Workflow 1: Triage & Executive Briefing ("What needs my attention right now?")
- **User Intent:** the operator logs in on mobile or desktop to quickly understand current status, review blocked tasks, check daily briefings, and unblock the fleet.
- **Pain Points Today:** Status is fragmented across `00 - Inbox/Briefing*.md`, the `attention` pane, `approvals`, and task health buckets.
- **Target Experience:** A unified "Morning Briefing / Command Feed" summarizing active lanes, high-priority blocks, pending approvals, and fleet throughput.

### Workflow 2: Multi-Agent Fleet Visibility ("What is each agent executing?")
- **User Intent:** Monitor multiple agents working simultaneously across repositories (`unified_agent`, `agent`, `ua-rust`, `browser`, `tcode`) without terminal thrashing.
- **Pain Points Today:** `AgentStatusBar` shows online/idle pills, but discovering which task an agent is modifying requires opening `sessions`, cross-referencing `tasks`, or running git commands.
- **Target Experience:** Interactive agent cards showing active repository, worktree, current claimed task, latest tool execution, and direct jump to terminal/session logs.

### Workflow 3: Action Approval & Governance ("Approve or deny tool calls")
- **User Intent:** A gated tool (`run_cmd`, external API, destructive write) is held by policy. The operator needs to vet the command safely and swiftly.
- **Pain Points Today:** Payloads are displayed as raw JSON. No explanation of *why* the agent needs the command or what happened immediately prior.
- **Target Experience:** Context-rich approval card displaying calling agent, parent task, command breakdown, risk assessment (read-only vs destructive), and 1-key keyboard actions (`A` to approve, `D` to deny).

### Workflow 4: Work Orchestration & Dispatch ("Move work forward")
- **User Intent:** Take an idea or customer discovery feedback, create an initiative/task, and dispatch a bounded background worker or developer lane.
- **Pain Points Today:** Creating a task in `tasks` and dispatching in `dispatch` are disconnected flows.
- **Target Experience:** Inline dispatch directly from task cards with preset templates, bounded timeouts, and progress streaming.

### Workflow 5: Accountability & Evidence Inspection ("Inspect proofs & memory")
- **User Intent:** Review completed work, verify test logs, inspect signed hash chains, and export client-ready evidence packets (PDF/CSV).
- **Pain Points Today:** Evidence pane is separate from task completion; verifying evidence requires opening multiple panes.
- **Target Experience:** Task detail view seamlessly embeds verification evidence, PR links, test summaries, and one-click PDF export.

### Workflow 6: Infrastructure & System Pulse ("Maintain operational health")
- **User Intent:** Verify that Tailscale, gbrain MCP, SQLite sync, and workers are healthy.
- **Pain Points Today:** Doctor, Devices, Telemetry, and Cycles live in separate top-level modes.
- **Target Experience:** Ambient status badge in the shell header (e.g. green dot `Hub & Fleet Healthy (0 warnings)`); clicking opens an inspector drawer.

---

## 4. Modern Product Benchmarks & Interaction Patterns

To upgrade the Control Center from an admin panel to a world-class productivity environment, we benchmarked 11 leading products:

| Product | Benchmark Pattern | Relevance to Unified Agent |
|---|---|---|
| **Docker Desktop** | **Container Cards & Action Bar:** High-density list with live status indicators, CPU/memory bars, instant restart/terminal/logs actions, and slide-over inspectors. | Ideal for **Agent Fleet Cockpit**: each agent (Claude, Agy, Codex, Hermes) presented as a live worker card with direct task inspection and session streaming. |
| **Notion** | **Command Palette (`Cmd+K`) & Clean Typography:** Universal search bar, breadcrumb navigation, distraction-free document views, and properties sidebars. | Essential for **Global Navigation & Memory Search**: jump instantly to any task, agent, memory note, or pane without touching the mouse. |
| **Clay** | **Dense Data Grid & Peek Preview:** Instant keyboard navigation (`J/K`), inline editing, floating action bars, and quick slide-over previews without page reloads. | Perfect for **Task Queue & Kanban**: review 50+ queue items rapidly, view subtasks in slide-over drawers, and edit properties inline. |
| **Spotify** | **Persistent Status Bar & Fluid Sections:** Global status bar always visible at the bottom/top; content organized into clear primary libraries. | Models the **Persistent Hub & Agent Bar**: active agent state and background cycle health remain visible across all navigation views. |
| **Gong** | **Timeline Highlights & Callout Snippets:** Chronological call streams with visual sentiment, key question markers, and action item extraction. | Inspires the **Session Activity Feed**: transform raw JSON SSE events into structured agent milestone timelines (claimed task, ran test, committed code). |
| **DoorDash** | **Urgent Order Queue & Time-to-Breach Timers:** Prominent countdowns for high-priority items, instant audio/visual alerts for new incoming items. | Applicable to **Attention & Approval Triage**: clear priority sorting with visual age indicators to prevent stuck tasks or forgotten approvals. |
| **Nooks** | **Ambient Spatial Presence:** Visual room layouts displaying who is speaking, idle, or deep in flow. | Informs **Agent Topology & Presence**: visualize the fleet as an active digital bullpen rather than static status labels. |
| **T3 Code / Moshi** | **Developer Ergonomics & Monospace Precision:** High contrast, zero animation latency, terminal-style telemetry, and dense keyboard shortcuts. | Guides **Visual Language & Theme**: preserve the developer-centric, high-trust technical feel without adding unnecessary consumer fluff. |
| **HubSpot / Salesforce** | **Pipeline Stages & Accountable Attribution:** Clear visual stages, audit logs, and owner assignment across complex multi-step lifecycles. | Enhances **Wayfinder & Initiative Tracking**: clear progression from discovery -> spec -> implementation -> test -> review -> deployment. |

---

## 5. Prioritized UX Problem & Opportunity Map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                      PRIORITIZED PROBLEM & OPPORTUNITY MAP                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ P1: 21 Flat Tabs            --> O1: Consolidated 4-Domain App Architecture   │
│ P2: Disconnected Agents     --> O2: Interactive Agent Fleet Drawer & Cards   │
│ P3: Context-Free Approvals  --> O3: Contextual Approval Action Cards         │
│ P4: Mouse-Heavy Navigation  --> O4: Global Command Palette (`Cmd+K`)         │
│ P5: Isolated Audit Proofs   --> O5: Embedded Verification Evidence in Tasks  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### High-Impact Problem 1: Navigational Fragmentation (21 Flat Tabs)
- **Problem:** Having 21 individual navigation modes creates extreme cognitive friction and visual clutter.
- **Opportunity 1 (Consolidated 4-Domain IA):** Group the 21 panes into 4 coherent functional domains with clear sub-navigation:
  1. **Overview & Triage:** Briefing Home, Attention Queue, Action Deck, Approvals.
  2. **Work & Execution:** Task Queue, Kanban Board, Dispatch Studio, Cycles Cockpit, Canvas.
  3. **Agents & Fleet:** Agent Cockpit, Live Sessions, Topology Graph, Flow DAG, Zulip Coordination.
  4. **System & Knowledge:** Memory Browser, Evidence Ledger, Doctor, Devices, Telemetry, LangGraph.

### High-Impact Problem 2: Passive Agent Presence
- **Problem:** `AgentStatusBar` is purely cosmetic; clicking an agent does nothing.
- **Opportunity 2 (Interactive Fleet Drawer):** Clicking an agent pill opens an interactive slide-over showing:
  - Current claimed task and repository worktree
  - Live activity log and latest tool invocation
  - Quick action to dispatch instructions or release claim

### High-Impact Problem 3: Approvals Lack Context
- **Problem:** Approvals show raw JSON without connecting the call to the agent's current objective or blast radius.
- **Opportunity 3 (Rich Approval Action Cards):** Render structured approval cards containing:
  - Agent badge, parent task title, and justification
  - Formatted command or diff with syntax highlighting
  - Safety rating (e.g. `Read-Only`, `Network External`, `Mutating Filesystem`)
  - One-click approve/deny with hotkeys (`A` / `D`)

### High-Impact Problem 4: Mouse-Heavy Workflows
- **Problem:** Moving between tasks, approvals, and search requires repeated mouse navigation.
- **Opportunity 4 (Command Palette `Cmd+K`):** Introduce a global command bar allowing operators to:
  - Jump to any pane or task by number (e.g. `#959`)
  - Filter by agent or status (`agent:claude`, `status:doing`)
  - Trigger global actions (`/dispatch`, `/doctor`, `/theme`)

### High-Impact Problem 5: Fragmented Verification & Evidence
- **Problem:** Evidence ledger is disconnected from the task view where operators review work.
- **Opportunity 5 (Embedded Verification & Export):** Embed evidence hashes, CI status badges, and one-click PDF/CSV export buttons directly inside the Task Detail panel.

---

## 6. Implementation Roadmap Alignment (#949–#953)

This discovery map provides the foundational input for the remaining tasks in roadmap #947:

1. **#949 (UX Architecture):**
   - Formalize the 4-domain information model.
   - Design route and URL structure (e.g. `/ui/triage`, `/ui/work`, `/ui/fleet`, `/ui/system`) while maintaining backward-compatible deep links.
2. **#950 (UX Design System):**
   - Establish shared UI component primitives (Docker-style Agent Cards, Clay-style Data Grids, Gong-style Timeline Items, Rich Approval Banners).
   - Ensure WCAG 2.2 AA compliance across all 3 themes (light, dark, high-contrast).
3. **#951 (UX Prototyping):**
   - Build interactive prototype components covering the Home Briefing, Fleet Drawer, and Command Palette.
   - Review prototype flows directly with the operator.
4. **#952 (UX Implementation):**
   - Incrementally refactor `packages/web` using the canonical SDK and TanStack Query keys without disrupting live operations.
5. **#953 (UX Dogfood & Polish):**
   - Validate the modern interface daily with the operator and desktop/laptop agents, tracking task completion latency and feedback notes.

---

## 7. Verification & Handoff

- **Artifact:** `docs/designs/ux-discovery-workflow-map.md` created in repository.
- **Empirical Grounding:** Validated against the live 21-mode SPA in `packages/web`, `routes.py`, `docs/web-ui.md`, and real dual-agent coordination workflows.
- **Standards:** Formatted with clean Markdown, structured tables, visual diagrams, and concrete actionable specifications.
