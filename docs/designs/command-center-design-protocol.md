# Command Center design protocol v1

> Partly superseded by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479): §3 (tokens) and §5 (truth states) still apply; for the shell, navigation, components and page templates, follow the v2 spec.

Status: accepted, 2026-10-07 (decisions in §10). Author: agent-claude.
Builds on agent-codex's task #1930 / draft PR #1060
([overhaul](frontend-overhaul-2026-10-07.md),
[wiring plan](frontend-overhaul-wiring-plan.md)). Reference rendering:
[command-center-reference.html](command-center-reference.html) (static sample
data, no hub calls; open it in a browser, press ⌘K, activate any Attention
row with Enter or Space, use `g` navigation chords, and resize it from desktop
to mobile). Overview has sample data; other destinations are labelled placeholders.
The inspector and palette demonstrate focus entry, trapping for overlays, and
restoration on close. Sample action dialogs explicitly send no requests.

This document is the contract a Control Center change is reviewed against:
principles, tokens, shell anatomy, the truth-state contract, components,
interaction, and the order to build it in. It replaces nothing in Codex's
information architecture or wiring gaps (G1–G12); it gives them one visual
system and closes the design problems the prototype left open.

## 1. Review of the Codex proposal

**Keep, as written.**

- The IA: five destinations (Overview, Delivery, Organization, Knowledge,
  Analytics) plus persistent Decisions, Messages, Audit and Dispatch. It folds
  39 peer views into questions an operator asks, and the migration table keeps
  every legacy link.
- The G1 finding is the most important thing in the PR. A failed read renders
  as "Nothing waiting" and "No agents registered". This protocol turns the fix
  into a component contract (§5) so it cannot regress pane by pane.
- One canonical work detail that runs, reviews, decisions, messages and receipts
  link to. This protocol gives it a shape: the inspector and the proof rail (§6).
- The wiring map and gap list, the refusal to let the UI imply authority it does
  not have, and the slice-by-slice delivery through existing gates.
- Mocha as the palette family, from the operator's own theme file.

**Change.**

| Problem in the prototype | Why it matters | Protocol decision |
|---|---|---|
| The action colour and the danger colour are the same hex (`--u-accent` = `--u-bad` = `#f38ba8`). the operator's theme sets `accent = red = #f38ba8`, and the prototype carried that collision in. | "Review decision" looks like an alarm and a blocked row looks clickable. Colour stops carrying meaning, which the existing design-system README forbids. | Action colour ≠ any state colour. Actions use Mocha **mauve** `#cba6f7`; rose `#f38ba8` means danger only. Rose stays the brand through the logo mark gradient, so the product still reads as the operator Mocha. (If the operator wants rose actions, danger must move hue. Either way the two never share one.) |
| Light theme swaps the accent to blue `#3153cb`. | The product changes identity when the theme flips. | Both themes use the mauve family: `#cba6f7` dark, `#7028d4` light, each contrast-checked (§3.2). |
| A parallel token set (`--u-*`) inside the prototype. | `packages/web` already has `--ua-*` tokens, `design-system/tokens.meta.json` and `build.mjs`, which fails on drift. A second vocabulary splits the system. | Extend `--ua-*` in `src/index.css`. Existing names keep their meaning; new names are additive (§3). |
| A marketing hero ("A clear view of the work.") with a WebGPU shader (2.55 MB bundle, animation never verified on a real adapter). | It costs about 160 px above the fold on the page whose job is triage, and spends motion on decoration. | No hero, no shader. The page title is the destination name. Liveness comes from data: the live stamp, the presence pulse, the change flash, and the status bar (§7). |
| Letter boxes (`O D T K A`) as nav icons. | Placeholder affordance; not scannable. | One icon set, Lucide (`lucide-react`, ISC, tree-shaken), 16 px, 1.75 stroke. |
| Sub-view tabs drawn as bordered buttons. | Tabs and buttons look the same, so the user cannot tell navigation from action. | Underline tabs. Buttons are the only bordered controls. |
| About 84 px rows, three equal stat cards with no trend or freshness, ids in the sans font. | A console reads low density as "less is happening". | Density modes (32/40/48 px rows), metric tiles with delta, sparkline and coverage, ids in mono. |
| No live fleet signal in the shell; the agent strip was removed rather than replaced. | "What are the agents doing now" is the command center's heartbeat. | A Fleet region on Overview plus a 28 px status bar on every page (§4). |
| Freshness is a stated principle but has no component. | Principles without components drift. | Every data region header carries a freshness stamp (§6.2). |

## 2. Principles

1. **Truth over reassurance.** A number appears only after the read that
   produces it succeeded. Unknown, partial, stale and denied each have their own
   look. Zero is a fact, never a fallback.
2. **Colour is spent on state.** Neutral surfaces, one action colour, six state
   colours. Every state colour comes with a glyph and a word.
3. **Decision first.** Every surface leads with what needs a human, who acts
   next, the consequence and the age.
4. **Every number is a door.** A count, tile or chart segment opens the exact
   records behind it.
5. **Dense and calm.** Small type, tight rows, generous gutters, hairline
   borders. No decoration that moves.
6. **Keyboard first.** Everything reachable from ⌘K; every destination has a
   `g` chord; lists move with `j`/`k`.
7. **One object, one place.** A task, run or decision has one inspector and one
   URL. Lists, boards and graphs are views onto it, never copies of it.

## 3. Tokens

`packages/web/src/index.css` stays the source of truth (it is what `build.mjs`
reads). Add the new names to `design-system/tokens.meta.json` with usage notes in
the same change. Components reference tokens, never hex; a raw hex literal
outside `index.css` and persona definitions is a review finding.

### 3.1 Colour: dark (default) and light

| Token | Role | Dark (Mocha) | Light (Latte, corrected) | Status |
|---|---|---|---|---|
| `ua-bg` | Page canvas | `#11111b` | `#eff1f5` | existing |
| `ua-surface` | Regions, sidebar, tables | `#181825` | `#ffffff` | existing |
| `ua-raised` | Inspector, palette, popovers | `#1e1e2e` | `#ffffff` + shadow | new |
| `ua-sunken` | Inputs, scope switcher | `#11111b` | `#e6e9ef` | new |
| `ua-hover` | Hover fill | `#232334` | `#e9ecf2` | existing |
| `ua-selected` | Selected row / nav, rail track | `#313244` | `#dce0e8` | new |
| `ua-border` | Hairline separation | `#2a2b3d` | `#dce0e8` | existing |
| `ua-border-strong` | Control boundaries (≥3:1) | `#6c7086` | `#7c7f93` | new |
| `ua-fg` | Primary text | `#cdd6f4` | `#4c4f69` | existing |
| `ua-muted` | Secondary text | `#a6adc8` | `#5c5f77` | existing |
| `ua-faint` | Captions, labels, table heads (on surface only) | `#9399b2` | `#6c6f85` | new |
| `ua-accent` | Primary action, selection, focus | `#cba6f7` | `#7028d4` | existing, re-valued |
| `ua-accent-fg` | Text on accent | `#11111b` | `#ffffff` | existing |
| `ua-link` | Links, metric strokes | `#89b4fa` | `#1650c8` | new |
| `ua-brand` | Logo mark only | `#f38ba8` | `#d20f39` | new |
| `ua-ok` | Done, verified, healthy | `#a6e3a1` | `#2f7d1f` | existing |
| `ua-live` | Running, in progress | `#89b4fa` | `#1650c8` | new |
| `ua-warn` | Waiting, stale, needs a look | `#f9e2af` | `#8a5a0c` | existing |
| `ua-decision` | A human must decide | `#fab387` | `#b54708` | new |
| `ua-danger` | Blocked, failed, denied, destructive | `#f38ba8` | `#d20f39` | existing |
| `ua-info` | Monitoring context | `#94e2d5` | `#0f6f75` | new |
| `ua-unknown` | Unknown / unavailable (with hatch) | `#7f849c` | `#6c6f85` | new |

Tints (`ua-ok-surface`, `ua-live-surface`, `ua-warn-surface`,
`ua-decision-surface`, `ua-danger-surface`, `ua-accent-surface`) are derived
once: `color-mix(in oklab, <state> 13–16%, var(--ua-surface))`. The
existing `ua-danger-surface` and `ua-danger-fg` keep their names. High contrast
keeps its current values; each new token maps onto them (states stay yellow
there, so the glyph rule in §6.1 carries the meaning).

Persona hues (`AgentPersonaBadge`) move onto Mocha accents that no state uses:
peach is excluded (it means decision), so personas use sky, sapphire, lavender,
mauve, teal, flamingo, rosewater and maroon. They appear only as the 3 px stripe.

### 3.2 Contrast (computed, WCAG 2.x)

| Pair | Dark | Light |
|---|---|---|
| `ua-fg` on `ua-surface` | 12.14 | 7.99 |
| `ua-muted` on `ua-surface` | 7.89 | 6.25 |
| `ua-faint` on `ua-surface` | 6.22 | 4.94 |
| `ua-accent` on `ua-surface` | 8.64 | 7.14 |
| `ua-accent-fg` on `ua-accent` | 9.23 | 7.14 |
| `ua-ok` / `ua-warn` / `ua-decision` / `ua-danger` on `ua-surface` | 11.81 / 13.81 / 9.92 / 7.58 | 5.15 / 5.92 / 5.43 / 5.43 |
| `ua-live` / `ua-info` on `ua-surface` | 8.34 / 11.79 | 6.96 / 5.91 |
| `ua-border-strong` on `ua-surface` | 3.59 | 3.95 |

Stock Latte green, yellow, peach, blue and teal fail 4.5:1 on white (2.6–4.3),
which is why the light column uses darkened values. `ua-faint` in light is 4.37
on `ua-bg`: keep captions on a surface. Slice 0 adds a vitest that recomputes
this table from `index.css` and fails under 4.5:1 for text pairs and 3:1 for
`ua-border-strong`.

### 3.3 Type

- **Geist Sans** for UI and **Geist Mono** for identifiers, both OFL,
  self-hosted from `@fontsource-variable/geist` and `…/geist-mono` (no runtime
  CDN in the Control Center app). Fallbacks: Inter, IBM Plex Sans, system-ui.
  The exported design-system preview is a separate distribution: its current
  `build.mjs` `FONTS_IMPORT` loads fonts from Google Fonts. Slice 0 must update
  that import, the forwarded `@theme` stacks, and `tokens.meta.json` font
  families together and document this preview-only CDN boundary. An app
  font change alone does not make the exported preview self-hosted.
- Scale (px, size/line): `label` 11/16 uppercase +0.06em · `caption` 12/16 ·
  `data` 13/18 (table cells) · `body` 14/20 (default) · `title-sm` 16/22
  (inspector) · `page-title` 20/28, −0.015em · `metric` 28/32, −0.02em.
- Weights 400, 500 (labels, buttons, row titles), 600 (titles, metrics).
- All numbers use tabular numerals. Task ids, agent labels, SHAs, paths and
  sequence numbers are always mono, verbatim, never prettified.

### 3.4 Space, shape, depth, motion

- 4 px grid: 4, 8, 12, 16, 20, 24, 32, 48. Page gutter 24, region gap 20,
  row padding 14–16 horizontal.
- Radius: `xs` 4 (pills, kbd), `sm` 6 (controls, nav items), `md` 8 (regions,
  tables), `lg` 12 (inspector, palette, dialogs), full (dots, avatars).
- Depth: in-flow surfaces use borders, not shadows. Overlays (palette, menus,
  dialogs, an overlaid inspector) take `--ua-shadow-overlay`. This is the one
  amendment to the README's "no shadow tokens" rule.
- Motion: `dur-1` 100 ms (hover), `dur-2` 160 ms (press, toggle), `dur-3` 220 ms
  (inspector, palette), ease `cubic-bezier(.2,.8,.2,1)`. Motion is allowed for
  exactly four things: overlay enter, the presence pulse, the change flash (a
  row whose state changed fades from its state tint over 1.2 s) and skeleton
  shimmer. `prefers-reduced-motion` removes all four.

### 3.5 Density

`data-density` on `<html>`, stored as `ua-web-density` next to `ua-web-theme`:
compact 32 px rows, default 40, comfortable 48. Touch pointers force at least
44 px targets regardless.

## 4. Shell anatomy

```
┌──────────┬──────────────────────────────────────────────┬────────────┐
│ Sidebar  │ Top bar: crumb · live stamp · ⌘K · Dispatch   │            │
│ 232 px   ├──────────────────────────────────────────────┤ Inspector  │
│ brand    │                                              │ 440 px     │
│ scope    │ Main (container-query responsive)            │ docks ≥1440│
│ 5 dests  │                                              │ overlays   │
│ Inbox:   │                                              │ below      │
│ Decisions│                                              │            │
│ Messages │                                              │            │
│ Audit    │                                              │            │
│ me/theme │                                              │            │
├──────────┴──────────────────────────────────────────────┴────────────┤
│ Status bar 28 px: hub · stream seq · agents · last event · build     │
└──────────────────────────────────────────────────────────────────────┘
```

1. **Sidebar.** Brand mark (rose → mauve gradient), scope switcher
   (workspace/project; one agent and an organization use the same objects),
   five destinations, an Inbox group (Decisions with a decision-coloured count,
   Messages, Audit), and the operator (identity, auth method, theme). `[`
   collapses it to a 56 px icon rail; it collapses automatically below 1280 px.
2. **Top bar.** Breadcrumb (destination › view › object), the global freshness
   stamp (Live · 2s / Reconnecting / Offline since 14:02), the ⌘K field, and one
   primary button: Dispatch. Nothing else is primary in the shell.
3. **Main.** Page header (title and one-line purpose, page-level controls on the
   right), underline tabs for contextual views, then regions. Layout responds
   to the main region's width with container queries, not to the viewport, so
   docking the inspector reflows the page correctly.
4. **Inspector.** Right-hand panel, `ua-raised`. Addressable by URL
   (`?inspect=task:1930`), so it is shareable and survives reload. It docks at
   1440 px and wider and overlays below; it becomes a full-screen sheet below
   768 px. Esc closes it and returns focus to the row that opened it.
5. **Status bar.** Mono, 11 px: hub connection, event-stream seq, agents online
   and running, age of the last event, density, deployed build. It is the
   ambient heartbeat and replaces the shader's job of making the console feel
   alive.
6. **Below 768 px.** A bottom tab bar with the five destinations, an Inbox sheet
   behind a badge, and Dispatch as the top-bar action.

## 5. The truth-state contract (G1 as a component)

Every block of hub data renders through one `<Region>` wrapper that takes the
query (or queries) and owns the state. A pane may not render a count, list or
chart outside it.

| State | Entered when | Renders | Never |
|---|---|---|---|
| `loading` | First read in flight | Skeleton rows at the final geometry | Spinner-only, "0" |
| `ready` | Every required read succeeded | Data with an "updated Ns ago" stamp | — |
| `empty` | Every required read succeeded with no rows | Fact sentence ending in a full stop ("No decisions waiting.") | Shown after a failed read |
| `partial` | Some sources failed or a page window was truncated | Available data plus a stamp saying what is missing | Totals presented as complete |
| `stale` | Last success is older than the region's freshness budget | Last known data, dimmed stamp in `ua-warn` with its age | Silent age |
| `denied` | 401/403 | "Your profile can't read <thing>." and the profile name | "Nothing waiting", "No agents registered" |
| `unavailable` | 5xx, network, timeout | Hatched block: what failed, the last known value and its age, retry | A zero, an empty list |

Rules: a count badge (sidebar, tab, tile) shows "—" with a tooltip unless its
region is `ready` or `empty`. `partial` and `stale` propagate up, so a tile
built from a partial region says partial. Each region declares a freshness
budget (live stream regions 30 s, polled 2× their interval). Tests cover every
state for every region (vitest with MSW handlers returning 200-empty, 401, 503
and a slow response).

## 6. Components

New components go in `packages/web/src/ui/`, are exported from `ui/index.ts`
and are added to `build.mjs` `COMPONENTS` with a guide in
`design-system/content/components/`.

1. **StatePill** (extends StatusIndicator). Six states, each a colour, a Lucide
   icon and an uppercase word: Decision ◆ (`ua-decision`), Blocked ▲
   (`ua-danger`), Running ◐ (`ua-live`), Waiting/Stale ◷ (`ua-warn`), Verified ✓
   (`ua-ok`), Unknown ? (hatched `ua-unknown`). The vocabulary is closed: a new
   status maps onto one of these or changes this document.
2. **FreshnessStamp.** "live", "updated 4s ago", "stale · 26h", "partial".
   It is in every region header and in the top bar. Relative under 24 h,
   absolute after; the exact timestamp is always in the tooltip.
3. **ProofRail.** The delivery chain as a fixed sequence of segments: Task →
   Run → Checks → Review → Decision → Merge/Deploy → Effect verified → Receipt.
   Compact horizontal form in tables (8 × 18 px bars); vertical form in the inspector
   with actor, detail and age per step. Unknown steps are hatched, not grey:
   "not started" and "we can't tell" must look different. The rail is the
   visual answer to "completion never means only a finished process".
   A hub receipt acknowledges that a request was recorded; it can exist before
   the outside effect and never makes Merge/Deploy or Effect verified green.
   Merge requires the host's merged PR record and exact merge SHA. Deployment
   requires the target's loaded revision, successful health check, and an
   affected-behavior check linked to that SHA and target. Other external actions
   require their destination's outcome record. Bind each observation's source,
   object/head, target, actor and time. Verified delivery requires these outcome
   observations as well as the earlier checks and decisions; an absent outcome
   stays unknown or blocked even when the request receipt is present.
4. **AttentionRow.** State pill · title · `id · owner → next actor ·
   consequence · age` · one action button. "you" is bold when the operator is
   next. The row opens the inspector; the button does the obvious next step.
   Lists sort by who acts next (operator first), then age.
5. **Inspector.** Header (mono id, state pill, open-full-page, close), eyebrow
   (object kind), title, one-line summary, a key/value list (owner, next actor,
   scope, head), a consequence callout in the state tint, the vertical
   ProofRail, then actions. Tabs below for Activity, Messages, Evidence and
   Raw. Raw is where ids, JSON and dependency editing live.
6. **AgentRow / AgentChip.** Persona stripe · mono label · current work or last
   signal · presence dot (live pulses, idle is an outline, overdue heartbeat is
   danger). The label always links to the agent's Organization record.
7. **CommandPalette.** It exists in `src/command-palette`; promote it. ⌘K
   searches actions, objects by id or title, agents, memory and destinations.
   Results are grouped and show the shortcut for each action. Every
   consequential action reached from it opens the same gated form as its
   button.
8. **MetricTile.** Label, value, unit or denominator ("8 / 11 · 73%"), delta,
   sparkline (`ua-link`, 1.5 px, no axes), and coverage. Tiles sit edge to edge
   in one bordered strip. Clicking one opens the exact cohort. Codex's G9 rules
   (defined numerator, denominator and interval) are a precondition for
   shipping any tile.
9. **DataTable** (existing). Adopts density row heights, mono id columns,
   right-aligned tabular numbers, a sticky header, and row-click to inspect.
10. **Button** (existing). Variants unchanged; primary uses the new accent. At
    most one primary per region; destructive actions use the danger variant
    and a confirm step that names the target.

## 7. Interaction

- **Shortcuts.** ⌘K palette · `.` Dispatch · `g o` Overview · `g d` Delivery ·
  `g t` Organization · `g k` Knowledge · `g a` Analytics · `g x` Decisions ·
  `g m` Messages · `g u` Audit · `j`/`k` move · Enter inspect · Esc close ·
  `[` sidebar · `?` shortcut sheet. Every chord is listed in the palette next to
  its action.
- **Change flash.** When the event stream changes an on-screen object, its row
  fades from the state tint over 1.2 s. With reduced motion, a static "updated"
  dot shows for 10 s instead.
- **Optimistic UI is not allowed for governed writes.** Approve, dispatch, send
  and lifecycle actions show pending until the server confirms. A refusal shows
  the server's reason inline at the control.
- **Focus.** 2 px `ua-accent` ring at a 2 px offset, inset −2 px on rows.
  Opening the inspector moves focus to its title; closing it restores focus.
- **Copy.** The existing content rules hold (sentence case, fact sentences for
  empty states, no exclamation marks). Add: "you" for the operator as next
  actor; never "All clear" unless every required source is `ready`.

## 8. Page templates

- **Overview.** Header, then the MetricTile strip (Needs your decision,
  Blocked, In flight, Verified delivery), then two columns: the Attention list
  and In flight table (ProofRail per row) on the left; Fleet and Health
  exceptions (rendering `stale` and `unavailable` honestly) in a 340 px column
  on the right. It collapses to one column under 1080 px of main width.
- **Delivery.** Tabs: Work · Board · Plans · Runs · Releases · Automation. The
  list on the left and the inspector on the right is the default; Board and
  graphs are alternative views of the same rows.
- **Organization.** Tabs: Agents · Teams · Authority · Resources ·
  Diagnostics. Lifecycle and config edits live in the inspector's Authority
  section behind their existing confirmations.
- **Knowledge.** Tabs: Memory · Vault · Tools · Skills · Proposals ·
  Connections. Memory rows lead with title and excerpt; the inspector shows
  content, sources, freshness, supersession and why it was recalled.
- **Analytics.** Tabs: Flow · Runs · Cost · Quality. Every chart states its
  cohort, interval and coverage in the header; segments open their records.
- **Decisions, Messages, Audit.** A full-height list and inspector, reached
  from the sidebar Inbox and from any object's inspector tab.

## 9. Delivery slices

Each slice is its own task and PR through exact-head CI and review. Before
coding, check hub claims on `App.tsx`, `shell/*`, `hub.ts` and `index.css`.

| Slice | Scope | Done when |
|---|---|---|
| 0. Tokens and type | `index.css` (new names, re-valued accent, Latte-corrected light), `tokens.meta.json` font families, app Geist via fontsource, contrast vitest, `build.mjs` font import and forwarded app `@theme` stacks, exported artifact regenerated | App self-hosts both font families without a font CDN; exported preview loads the documented Geist family via its separate import and has matching sans/mono variables; contrast test green in all three themes; no raw hex outside `index.css` |
| 1. Truth states | `<Region>`, FreshnessStamp, StatePill; fix AttentionBadge and AgentStatusBar (G1) | 401, 503, empty and slow fixtures each render their own state; no "Nothing waiting" after a failed read |
| 2. Shell | Sidebar, top bar, status bar, lucide icons, `nav.ts` remap with every legacy id and alias resolving, mobile tab bar, density setting | All 39 legacy ids deep-link; axe passes; 320–1920 px with no horizontal overflow; keyboard chords work |
| 3. Inspector and ProofRail | URL-addressable inspector; ProofRail fed by G2/G3 joins | One task shows a correct rail for success, failed run, blocked release and missing receipt |
| 4. Overview | Template from §8 on live queries | Default view answers what needs me, why, who is next and how old it is |
| 5+. Destinations | Delivery, Organization, Knowledge, Analytics, Inbox panes following Codex's slices 2–5 and gaps G2–G12 | Per Codex's review checkpoints plus §5 state coverage |

Slices 0 and 1 have no IA dependency and can start now. Slice 2 needs the operator to
agree the destination names in PR #1060.

## 10. Decisions (accepted 2026-10-07)

The operator accepted the recommendations on 2026-10-07:

1. **Accent hue.** Mauve actions; rose is danger and the brand mark only.
   Action and danger never share a colour.
2. **Font.** Geist Sans and Geist Mono, self-hosted.
3. **Shader.** Dropped. Liveness comes from data (§7).

Slice 0 is task #1942 and slice 1 is task #1943, both under #1930.
