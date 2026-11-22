The Control Center is an operator's console for a fleet of coding agents: dense, calm, keyboard-first, and legible in three themes. Every surface answers one question — *what needs me now?* — so colour is spent on state, never on decoration.

## Content fundamentals

- **Plain, operational, sentence case.** Headings and buttons name the thing or the action: "Task Queue", "Review and publish", "Check surface drift", "Sign Out". No marketing voice, no exclamation marks, no emoji in copy.
- **Empty states state the fact and end with a full stop:** "No open sessions.", "No goals yet.", "No crew messages yet." Loading states use an ellipsis character: "Loading catalog…".
- **Uppercase is reserved for labels**, never sentences: `eyebrow` ("UNIFIED AGENT"), `section-label` ("THEME"), `table-head`, Badge text and `status-label` ("RUNNING", "BLOCKED").
- **Identifiers are shown verbatim in `mono`:** agent labels (`agent-claude`), task ids (`#1131`), ULIDs, paths. Never prettify an id an operator may paste.
- **Every control has an accessible name** that says what it acts on: "Sign out of Control Center", "Close approval detail", "Approvals filters". Scroll regions say so: "Fleet hosts (scrollable)".

## Visual foundations

### Colour

- Three themes ship together: **Dark** (the default `:root`, Catppuccin Mocha), **Light** (Catppuccin Latte with its state hues darkened to 4.5:1; also used when the OS prefers light and no choice is stored) and **High contrast** (black ground, white, yellow and cyan ink). The choice is stored as `ua-web-theme` and applied as `data-theme` on `<html>`. Always reference tokens (`var(--ua-…)`), never raw hex, so all three themes work. The full contract is `docs/designs/command-center-design-protocol.md`.
- Grounds: `ua-bg` for the page canvas, `ua-surface` for regions (sidebar, header, pane panel, cards, tables), `ua-raised` for overlays (inspector, palette, dialogs), `ua-sunken` for inputs. `ua-hover` is the only hover fill; `ua-selected` marks the selected row or nav item.
- Text is `ua-fg`; secondary text is `ua-muted`; captions and labels are `ua-faint`, which belongs on a surface (it is 4.4:1 on the light canvas).
- `ua-accent` (mauve) is the single action colour: primary buttons, pressed chips, the selected tab and focus rings. It is never a state. Put `ua-accent-fg` on it, never `ua-fg`.
- State colours mean state only: `ua-ok` (done, verified, approved), `ua-live` (running, in progress), `ua-warn` (waiting, pending, stale), `ua-decision` (a human must decide), `ua-danger` (blocked, failed, denied, destructive), `ua-unknown` (unknown or unavailable, always hatched). Each has a `*-surface` tint for pills and callouts. Failures are danger, not amber.
- Rose belongs to danger. `ua-brand` repeats it for the logo mark only, so it never sits on a control or a status.
- State is never colour alone: StatusIndicator pairs each colour with a glyph (● ✓ ◐ ▲ ○) and an uppercase word. This matters most in high contrast, where accent, warn, decision, danger and muted are all yellow.
- `ua-border` is a hairline for separation. Control boundaries use `ua-border-strong`, which clears 3:1 on `ua-surface`.
- Agent identity uses the `persona-*` hues (Catppuccin Latte accents that no state uses) as a 3px left stripe on AgentPersonaBadge only. They are marks, never text or fills.

### Type

- **Geist Sans** (`--font-sans`) for the interface and **Geist Mono** (`--font-mono`) for identifiers. The app self-hosts both from `@fontsource-variable` (no font request leaves the hub); this system loads them from Google Fonts in `bundle.css`. Fallbacks: Inter, IBM Plex Sans, system-ui.
- The UI runs small: `body` (14/20) is the default, `caption` (12/16) for metadata. Weights are 400, 500 (labels, buttons) and 600 (titles); 700 only for `page-title`.
- Titles step `app-title` (20) → `pane-title` (18) → `card-title` (16). Each pane opens with exactly one `pane-title` h2.
- Numbers that change in place (`stat-value`, counts in chips) use tabular numerals.

### Space, shape, depth

- A 4px grid via the Tailwind scale: `space-2` (8px) and `space-3` (12px) do most of the work; `space-4` pads panels and cards; `space-6` pads empty states.
- Radii step with the size of the thing: `radius-sm` for controls, `radius-md` for the pane panel and tables, `radius-lg` for cards, `radius-full` only for status dots and persona pills.
- **Borders, not shadows, for anything in the page flow.** Elevation is `ua-surface` on `ua-bg` with a `ua-border` hairline. Only overlays (command palette, menus, dialogs, an overlaid inspector) take a shadow, which separates them in light where `ua-raised` equals `ua-surface`.
- Layout is a sidebar and a work column (design protocol §4). The sidebar holds five destinations (Overview, Delivery, Organization, Knowledge, Analytics), the Inbox (Decisions, Messages, Audit), the operator and the theme. It becomes an icon rail below 1280px or on `[`, and a scrolling strip on phones. The work column holds a top bar (breadcrumb, attention pill, ⌘K field, Dispatch), the page title, the destination's views as underline tabs, one bordered pane panel in `<main>`, and a 28px status bar (hub reachability, agent counts). Agent presence shows on Overview.

### Interaction and states

- **Focus:** every focusable element shows a solid `ua-accent` outline, `focus-ring-width` (3px) at `focus-ring-offset` (2px). Table rows inset it (-2px). It holds 3.3:1 or better against `ua-bg` and `ua-surface` in every theme.
- **Hover:** secondary and ghost controls fill with `ua-hover`; primary dims to `primary-hover-opacity`; interactive cards take an `ua-accent` border plus `ua-hover`.
- **Disabled and busy:** `disabled-opacity` and `not-allowed` cursor; a busy Button swaps its left icon for a spinner and sets `aria-busy`.
- **Motion** is limited to colour transitions, the busy spinner and an optional status pulse, which stops under `prefers-reduced-motion`.
- **Keyboard:** Enter and Space activate buttons, chips and interactive cards; Backspace or Delete removes a removable chip; DataTable rows use a roving tabindex with Arrow, Home and End.
- Every page starts with a "Skip to main content" link that appears on focus. The bar is WCAG 2.2 AA with zero critical or serious axe violations in all three themes.

## Iconography

- Shell icons come from `lucide-react` (16px, 1.75 stroke), always `aria-hidden` next to a visible or screen-reader label. State meaning is still carried by Unicode glyphs inside status dots (● ok, ✓ done, ◐ in progress, ▲ warning or failure, ○ idle) and the × on removable chips, always next to a word.
- Drawn icons are the Lucide shell icons (sidebar destinations, search, Dispatch, sign-out, theme, collapse) and the Button's busy spinner (inline SVG in `currentColor`).
- Agent personas carry an emoji avatar in AgentPersonaBadge; that is the one place emoji appear, and it is data from the persona, not UI decoration.
- The mark is a 24px "ua" tile on a `ua-brand` → `ua-accent` gradient beside "Unified Agent" in the sidebar (the label hides in the icon rail). The page `h1` "Control Center" is screen-reader only; each view's visible title is its `h2`.

## Components

The primitives below are built live from `packages/web/src/lib/ui` and exposed as `window.UnifiedAgent` (React). Reach for these before writing new markup:

| Need | Use |
|---|---|
| An action | `Button` — `primary` once per view, `secondary` by default, `danger` for destructive, `ghost` in dense rows |
| A read-only label or count | `Badge` |
| A filter or toggle | `Chip` (`pressed`, `count`, `onRemove`) |
| A grouped block of content | `Card` with `Card.Header` / `Title` / `Description` / `Body` / `Footer` |
| Rows of records | `DataTable` |
| The state of an agent, task, run or approval | `StatusIndicator` |
| Who did it | `AgentPersonaBadge` |
| Go to a page from a button-shaped control | `LinkButton` (a real link) |
| An icon-only action | `IconButton` (`aria-label` required) |
| A labelled form control | `Field` around `Input`, `Textarea`, `Select` or `Combobox`; `Checkbox`, `Switch` and `RadioGroup` carry their own labels |
| A modal task, a side detail, a confirmation | `Dialog`, `Drawer`, `ConfirmDialog` (never `window.confirm`) |
| A short list of actions behind one button | `Menu` |
| Sections that are URLs; sections inside one page | `LinkTabs`; `Tabs` |
| The top of a page | `PageHeader` (the page's one `h1`) |
| Search and filters above a list | `FilterBar` inside a `Toolbar` |
| Loading, failed or empty data | `Skeleton`, `ErrorState`, `EmptyState` (exactly one per region) |
| A short confirmation or failure after an action | `ToastProvider` and `useToast` |
| Label and value pairs; code; a note | `DescriptionList`; `CodeBlock`; `Markdown` (sanitized) |
| A graph of linked items | `GraphCanvas` (app only, with a table view) |
