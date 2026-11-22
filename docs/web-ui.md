# Web control center

The hub serves the Control Center at `/ui`. It is a React single-page app
built from [`packages/web`](../packages/web/). Since the Control Center v2
cutover (task #2479) `/ui` renders the v2 app: eight destinations, real URLs
for every page, and a router. The 39-view classic app is deleted, and its
`?view=` links redirect to the new pages (see
[Classic links](#classic-links)). The build contract is
[Control Center v2 spec](designs/control-center-v2-spec.md).

The app talks to the same authenticated `/api` surface as before. The cutover
changed the client, not the API contracts, the identity plane, the CSRF model
or the event-stream cursor described below. The routes v2 added for its write
pages are documented in [Control Center API](control-center-api.md).

The UI is a policy-aware orchestration surface, not an execution engine.
Browser actions go through the registered MCP tools, identity checks, CSRF
protection, capability policy and approval gates. Cycles record lifecycle
state and budgets only; they do not execute work, merge, deploy, push or run
arbitrary shell commands.

## Open it

The UI is part of the hub; there is nothing extra to start.

```bash
systemctl --user restart unified-agent
xdg-open http://127.0.0.1:8765/ui
```

Over Tailscale:

```bash
tailscale serve --bg --tcp 8765 127.0.0.1:8765
```

The hub still binds loopback only; `tailscale serve` is what exposes it. Add
the tailnet name to both `UA_HTTP_ALLOWED_HOSTS` and `UA_HTTP_ALLOWED_ORIGINS`.

`/ui` and `/ui/` land on `/ui/home`. Every page has its own address, so a
reload, a bookmark, a shared link, middle-click and back and forward all work.

### Single mount and deploy

`webui/spa.py` plus `web_routes()` (locked by
`tests/test_webui_route_inventory.py`) wire one mount:

- `/ui` and `/ui/{path:path}` serve the build with an SPA fallback:
  extensionless paths return `index.html`, real missing assets `404`. The
  server does not know the app's routes; the client decides which page
  renders. This is why an id that can contain `.` or `/` (a vault path, a
  memory key) goes in the query string, never in the path.
- The build is the one packaged inside an installed wheel, or
  `packages/web/dist` in an editable source checkout. `UA_WEB_DIST_DIR`
  overrides it for a custom or development UI (a stale path keeps serving an
  older build).
- The retired prefixes `/ui/legacy*` and `/ui/excalidraw/*`, and any `..`
  traversal segment, `404` explicitly rather than reaching the SPA fallback.
- **When `index.html` is absent, `/ui` is a hard `503`** with a build hint
  (`npm --prefix packages/web run build`). There is no fallback UI.

The UI therefore only appears once a build reaches the hub. The cockpit's
`scripts/deploy-mac-hub.sh` builds the three JS packages in order
(`protocol`, `client`, `web`) and copies `packages/web/dist` to the hub on
every run; `UA_SKIP_WEB_BUILD=1` skips that
step (`/ui` then answers 503 until a build lands). After a deploy,
hard-refresh once to drop any cached shell HTML.

### Bundle layout: `assets/` and `vendor/`

The build writes two content-addressed directories, and `serve_modern` sends
both with a one-year immutable cache:

- `dist/assets/` holds the app's own chunks, hashed per file. Each destination
  is its own lazy chunk. The planning canvas page
  (`src/app/work/CanvasPage.tsx`) dynamic-imports Excalidraw through
  `src/lib/canvas/ExcalidrawHost.tsx`, so the diagramming stack is fetched only
  by someone who opens `/ui/work/canvas`.
- `dist/vendor/<name>-<hash>/` holds prebuilt dependency files that are a
  *single* module larger than Rollup's 500 kB advisory, so no chunking strategy
  can split them. `packages/web/vite.config.ts` externalizes them and copies
  them here, and the browser fetches them only on the cold path that needs
  them: the harfbuzz WASM payload Excalidraw uses to subset fonts for an
  export, and the Langium runtime mermaid uses to parse a diagram. Together
  that is about 3.1 MB kept out of the bundle graph.

`npm run build` is expected to finish with **no** Rollup chunk-size warning.
If one appears, a dependency upgrade has renamed or grown one of these files:
fix the split or the externalized path in `vite.config.ts`. Do not raise
`build.chunkSizeWarningLimit`.

### Developing against a live hub

`npm run dev` in `packages/web` serves the app on loopback
(`http://127.0.0.1:5173/ui/`) and, on its own, proxies nothing. To see live
hub data, name the hub explicitly (task #1579):

```bash
UA_DEV_HUB_URL=http://<hub-host>:8765 npm run dev
```

`/api`, `/healthz`, `/oauth` and `/mcp` are then proxied to that origin
(`packages/web/dev-proxy.ts`). The app keeps calling them same-origin, as it
does when the hub serves `dist/`.

- One machine runs the production hub. `UA_DEV_HUB_URL` names that hub (for
  example over Tailscale). Do not stand up a second production hub on a
  development machine.
- While the proxy is on, the dev server binds `127.0.0.1`. `--host` (or any
  non-loopback `server.host`) is refused, because the proxy carries the hub
  session cookie and OAuth sign-in.
- A hub URL with `user:password@` is refused. Sign in through the UI.
  Writes still need the CSRF token from `GET /api/whoami`.
- The proxy rewrites `Host` but forwards the browser's `Origin`
  (`http://127.0.0.1:5173`), and the hub checks `Origin` separately. Add that
  exact origin to the hub's `UA_HTTP_ALLOWED_ORIGINS`, keeping the existing
  entries (a non-empty value replaces the default list), and restart the hub.
  Without it, sign-in fails with `403 {"error":"origin not allowed"}`.
- Only `http` and `https` URLs are accepted. The path is ignored; the proxy
  targets the hub's origin.

Full script table and constraints: [packages/web/README.md](../packages/web/README.md).
If pages stay empty or Vite refuses `--host`, see
[troubleshooting](troubleshooting.md#control-center-npm-run-dev-has-no-live-data).

### Stack

React 19, Vite, TypeScript, Tailwind v4, TanStack Query v5, react-router v7
(`createBrowserRouter`, basename `/ui`), `@xyflow/react` for graphs, and the
`@excalidraw/excalidraw` 0.18.1 canvas. The app depends on the workspace
packages `@unified-agent/protocol` and `@unified-agent/client`. Exact versions
are in `packages/web/package.json`.

## Destinations and routes

The sidebar shows eight destinations, in this order. The code list is
`DESTINATIONS` in
[`packages/web/src/app/routes.ts`](../packages/web/src/app/routes.ts).

| Destination | Path | What it answers | Shortcut |
|---|---|---|---|
| Home | `/ui/home` | What needs me, what is running, what just finished | `g h` |
| Inbox | `/ui/inbox` | Everything waiting on a person, with the action inline | `g i` |
| Work | `/ui/work` | Tasks, goals and plans | `g w` |
| Agents | `/ui/agents` | Who is in the organization and what each can do | `g a` |
| Sessions | `/ui/sessions` | Live and past agent sessions | `g s` |
| Knowledge | `/ui/knowledge` | Memory notes and the vault | `g k` |
| Runs | `/ui/runs` | Workflows, graphs, routines and cycles, with their history | `g r` |
| Settings | `/ui/settings` | Health, cloud, usage, credentials, integrations, audit | `g t` |

### Route table

Every route below is under `/ui`. `ROUTE_SAMPLES` in `routes.ts` lists one
concrete sample per route, and `ROUTE_REDIRECTS` the two redirects. Tests walk
both lists (see [Source layout and tests](#source-layout-and-tests)).

| Path | Page |
|---|---|
| `/home` | Home: Needs you, Running now, Just finished, the agent strip, and a first-run checklist until the organization has an agent, a session, a task and a note. `?project=` scopes the page. |
| `/inbox` | The inbox list with a detail pane. `?kind=approval\|message\|hold\|proposal\|anomaly` filters, `&item=<id>` selects. |
| `/inbox/messages` | Conversations between agents and the operator: send, reply, hand off to a task. |
| `/work` | One task set in four views, `?view=list\|board\|tree\|graph`, plus filters. `&scope=work` shows the work graph. |
| `/work/tasks/new` | Create a task. |
| `/work/tasks/:id` | Task page: edit in place, status, claim, subtasks, dependencies, activity, comments, evidence, work products. |
| `/work/goals`, `/work/goals/:id` | Goals and one goal with its linked tasks. |
| `/work/plans`, `/work/plans/new`, `/work/plans/:id` | Plans, create a plan, one plan with steps, checkpoints and shared-plan review. |
| `/work/canvas` | The Excalidraw planning canvas. |
| `/agents` | Roster. |
| `/agents/add` | Add a local or cloud agent. |
| `/agents/teams` | Teams: start a team; `?tab=registry` for registry teams, installing a team revision and starter team templates; `?tab=operating` for operating profiles, department bindings, sprints and per-run settings. |
| `/agents/skills` | Skills: published versions, comparing a skill directory with the registry (`?surface=`), installing and publishing a skill. |
| `/agents/org` | Organization chart and policy revisions with rollback (`?rev_kind=policy\|profile\|approval_rule\|org`). |
| `/agents/topology` | Agent topology graph. |
| `/agents/hosts` | Machines (with restarting the executor on the hub's machine), agents with no machine, devices (ua-nodes) with acceptance and attestation, signed-in devices and revoking them, approver passkeys, harness configuration. |
| `/agents/capabilities` | The capability catalog. |
| `/agents/profile/:label` | One agent. Never `/agents/:label`, because a label may equal a fixed word. |
| `/sessions` | Sessions list, `?status=open\|ended\|all`. `?tab=work\|devices\|teams\|activity` shows work sent to devices, sessions found on devices (adopt), team snapshots, and the live activity feed. |
| `/sessions/new` | Start a session (dispatch). |
| `/sessions/:id` | One session: transcript, composer, actions. |
| `/knowledge` | Redirects to `/knowledge/memory`. |
| `/knowledge/memory` | Memory notes, `?q=`, `&tag=`, `&note=<key>`. |
| `/knowledge/vault` | The vault, `?path=<vault path>`, `&q=`. |
| `/runs` | Overview: running now and recent runs. |
| `/runs/workflows` | Reviewed workflows; run one. |
| `/runs/graphs` | LangGraph runs and checkpoints. |
| `/runs/routines` | Scheduled routines. |
| `/runs/cycles`, `/runs/cycles/:id` | Autonomous cycles and one cycle. |
| `/runs/receipts` | Delivery receipts. |
| `/runs/telemetry` | Run waterfall and exception digest. |
| `/runs/insights` | Flow metrics, the task graph, and one task's lineage with `?task=<id>`. |
| `/runs/executor` | Executor health and delegations. |
| `/settings` | Redirects to `/settings/health`. |
| `/settings/health` | The doctor report. |
| `/settings/platform` | Object-model manifest. |
| `/settings/cloud` | Cloud worker health. |
| `/settings/usage` | Quota and delivery SLO. |
| `/settings/credentials` | Credential pool and session credentials. |
| `/settings/audit` | The evidence ledger. |
| `/settings/audit/provenance` | The provenance graph query. |
| `/settings/integrations` | Connectors. |
| `/settings/integrations/slack`, `/zulip`, `/notifications` | Slack gateway, Zulip, notification delivery. |
| `/settings/appearance` | Theme and density. |

Filters, the selected tab and the selected row live in the URL, so a reload or
a shared link restores the view. Any other path renders a not-found page with
links to the eight destinations.

## Shell

The shell lives in `packages/web/src/app/shell/`: one sidebar, one top bar,
one main region and a status bar.

- **Sidebar** (`Sidebar.tsx`): the eight destinations with icon and label.
  Inbox carries a count from a complete read only; while that read is unknown
  it shows "–", never 0. The sidebar collapses to an icon rail on narrower
  screens or with `[`. Phones get a bottom tab bar (`BottomTabBar.tsx`) with
  Home, Inbox, Work, Sessions and "More".
- **Top bar** (`TopBar.tsx`): a breadcrumb built from route handles, a search
  field that opens the command palette, a "New" menu (Task, Session, Plan,
  Memory note, Agent), and the account menu (who is signed in, theme, sign
  out).
- **Main**: `<main id="main-content">`. Each page renders its own
  `PageHeader`. Areas with sub-pages render section tabs as links under the
  header (`role="navigation"`, because each tab is a URL).
- **Status bar** (`StatusBar.tsx`): the live-update connection state, when the
  data last checked in, and the hub version from `GET /api/whoami`.
- **Command palette** (`CommandPalette.tsx`): `Cmd/Ctrl+K` or `/`. It lists
  the destinations, the "New" actions, every fixed page, theme commands and
  each area's own commands (exported from its `routes.tsx`). It only
  navigates or switches theme; it never changes a task or an approval.
- **Shortcuts** (`useAppShortcuts.ts`): `g` then `h i w a s k r t` for the
  destinations, `c` create a task, `/` search, `?` shortcut help, `[`
  collapse the sidebar. They never fire inside an input, textarea, select or
  contenteditable.
- **Focus and title.** On navigation, focus moves to the page `h1` and the
  document title becomes `<page> · Control Center`. The skip link stays first
  in the tab order.
- **Theme.** Light, dark and high contrast, stored in `localStorage` key
  `ua-web-theme` and applied as `data-theme` on `<html>`. The first load
  follows `prefers-color-scheme`. `/ui/settings/appearance` adds "System"
  (follow the operating system) and a density choice, kept on this device.

`/ui/?showcase=primitives` renders the design-system primitives on one page
(`src/lib/ui/ShowcasePage.tsx`) for their accessibility spec,
`tests/ui.a11y.spec.ts`. It is not linked from the app.

### Sign-in and session expiry

The app keeps no bearer token in `localStorage` or `sessionStorage`. It uses
the same HttpOnly cookie session as the rest of `/api`:

1. `GET /api/whoami` with `credentials: "same-origin"`. Until it first
   answers, the shell shows "Checking who is signed in" and mounts no page. A
   **401** renders `SignInView` (`src/lib/auth/SignInView.tsx`). A 403 skips
   whoami retries but does not open the form (it is not an expired session).
2. The form POSTs `{ token }` to `/api/session`. The hub sets the session
   cookie and returns `{ agent, csrf }`. When `GET /api/session/options`
   returns `{"workos": true}`, the same screen also offers **Sign in with
   WorkOS** (task #2114): `GET /api/session/workos/login` redirects to AuthKit
   for the hub's one organization (signed, browser-bound state and PKCE); the
   callback verifies membership and the identity map, then mints the same
   `ua_ui` cookie. Writes re-check membership live; approvals stamp
   `workos-idp-v1`. Hosted public hubs never enable the passphrase fallback,
   even if `UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1` is still set
   (`config.hosted()`).
3. After sign-in the person lands on the URL they asked for. A token sign-in
   happens in place. A WorkOS sign-in is a full navigation and the hub's
   callback lands on `/ui/`, so the sign-in gate stores the requested path
   first (`src/app/lib/returnTo.ts`) and `src/main.tsx` restores it when the
   browser comes back. Only a same-origin `/ui/<segment>` path is stored, it
   expires after 10 minutes, and it is consumed once, so it cannot become an
   open redirect.
4. Sign-out is `DELETE /api/session`, sent with the CSRF token of the session
   the cookie names; the hub answers 403 `csrf_session_mismatch` and revokes
   nothing otherwise.

`unified-agent doctor --whoami` shows the identity a credential maps to.
Consent stays pinned to the public host used to mint the token: a token for one
host cannot be replayed against another. Client credentials (non-interactive
cloud-bot auth) exist but are off by default. Refresh tokens rotate and
revocations are audit-logged.

## Writes

Every write in the app goes through `useHubWrite` in
`packages/web/src/app/lib/mutations.ts`. The rules are in spec §6; in short:

- **CSRF.** The token comes from `GET /api/whoami` and is sent as
  `x-ua-csrf`. A 403 that names CSRF triggers one token refresh and retry.
- **Outcomes.** A write ends as done, accepted (202 for work that has
  started, such as a dispatch), waiting for approval, or an error. A parked
  approval is recognized the way the hub marks it (a 202 with
  `code: "approval_required"` or the other approval fields listed in spec
  §6). **A parked write is shown as "Waiting for approval", never as
  success**, with a link to `/ui/inbox?kind=approval&item=<id>`. The form
  keeps its contents and nothing changes optimistically.
- **Approval does not run the write.** An approved row is a one-shot pass for
  the identical call. The UI never re-sends by itself. A resumable write is
  stored (`src/app/lib/parkedWrites.ts`), and the form and the Inbox item both
  offer **Run it now** once it is approved. Writes whose body carries a
  secret, session launches and work-product submissions are never resumable;
  their form says to submit again after approval.
- **Errors.** 401 goes to sign-in. 403 says the signed-in identity may not do
  this. 409 says the object changed and offers Reload. The raw transport
  message stays in a collapsed detail. Destructive writes always open a
  confirm dialog.
- **Drafts.** Forms keep a draft (`src/app/lib/drafts.ts`) keyed by the
  signed-in identity, the operation and the object, until the write is done.
  Tokens and credentials are never written to a draft.
- **Owner epoch.** All app data, drafts, stored writes and toasts belong to
  one verified owner (hub origin, organization and identity) per tab
  (`src/app/lib/epoch.tsx`). A different identity, a sign-out, a 401 or a
  sign-in in another tab revokes the epoch: its queries stop, its storage
  reads return nothing, and its late completions do nothing. The page tree is
  keyed by the epoch, so the next owner never sees the previous owner's state.
  `tests/app/architecture.test.ts` and `tests/app/lib/owner-switch.test.tsx`
  enforce this.

On the server side, every `/api/action/*` route turns a tool's own refusal
(`ToolError`) into a 400 with the tool's message, so an operator mistake
("no workflow definition 'x'; available: ...") reaches the page instead of a
bare 500. Typed refusals are listed under
[Typed write errors](#typed-write-errors-2056).

## Classic links

Classic links still work. Before the router starts, `src/main.tsx` calls
`legacyRedirect()` (`packages/web/src/app/lib/legacyRedirect.ts`), which
rewrites `/ui/?view=<id>` and `/ui/#<id>` with `history.replaceState` to the
new path. The WorkOS return path is restored first; a page that comes back
from sign-in is never rewritten.

| Classic `?view=` | New path |
|---|---|
| `home` | `/ui/home` |
| `attention` | `/ui/inbox` |
| `approvals` | `/ui/inbox?kind=approval` |
| `messages` | `/ui/inbox/messages` |
| `proposals` | `/ui/inbox?kind=proposal` |
| `tasks` | `/ui/work` |
| `board` | `/ui/work?view=board` |
| `task-graph` | `/ui/work?view=graph` |
| `work-graph` | `/ui/work?view=graph&scope=work` |
| `goals` | `/ui/work/goals` |
| `plans` | `/ui/work/plans` |
| `canvas` | `/ui/work/canvas` |
| `sessions` | `/ui/sessions` |
| `dispatch` | `/ui/sessions/new` |
| `org` | `/ui/agents/org` |
| `agent-topology` | `/ui/agents/topology` |
| `skills-teams` | `/ui/agents/teams` |
| `operating` | `/ui/agents/teams?tab=operating` |
| `capabilities` | `/ui/agents/capabilities` |
| `devices` | `/ui/agents/hosts` |
| `memory` | `/ui/knowledge/memory` |
| `vault` | `/ui/knowledge/vault` |
| `cycles` | `/ui/runs/cycles` |
| `langgraph` | `/ui/runs/graphs` |
| `executor` | `/ui/runs/executor` |
| `telemetry` | `/ui/runs/telemetry` |
| `dispatch_receipts` | `/ui/runs/receipts` |
| `flow` | `/ui/runs/insights` |
| `doctor` | `/ui/settings/health` |
| `platform` | `/ui/settings/platform` |
| `cloud` | `/ui/settings/cloud` |
| `quota` | `/ui/settings/usage` |
| `credential_pool` | `/ui/settings/credentials` |
| `evidence` | `/ui/settings/audit` |
| `provenance` | `/ui/settings/audit/provenance` |
| `connectors` | `/ui/settings/integrations` |
| `slack_gateway` | `/ui/settings/integrations/slack` |
| `zulip` | `/ui/settings/integrations/zulip` |
| `notify_delivery` | `/ui/settings/integrations/notifications` |

Aliases resolve to the id they name: `actions`, `decisions` → `approvals`;
`portfolio`, `overview` → `home`; `queue`, `delivery` → `tasks`; `kanban` →
`board`; `topology` → `agent-topology`; `organization` → `sessions`;
`knowledge` → `memory`; `analytics` → `flow`; `audit` → `evidence`. Ids are
matched case-insensitively.

Rules:

- Only the landing (`/ui` or `/ui/`) is rewritten. A path with a segment is
  already a v2 route.
- An unknown `?view=` id goes to `/ui/home`. A hash that is not a view id
  (`#main-content`) is left alone.
- Other parameters carry over. Three are renamed or moved into the path:
  `approval=<n>` on `approvals` becomes `item=<n>`
  (`/ui/inbox?kind=approval&item=<n>`); `task=<n>` on `tasks` becomes
  `/ui/work/tasks/<n>`; `plan=<id>` on `plans` becomes `/ui/work/plans/<id>`.
  Everything else is kept as it is, because the new pages read the same
  names: `task` on `flow` (Insights lineage), `task_id` and `item` on
  `evidence` (Audit filters), `path` on `vault`.
- The target's own parameters (`kind`, `view`, `scope`, `tab`) win over
  carried ones.

The hub's own links already point at v2 pages: approval push notifications,
Slack approval cards and MCP URL-mode elicitation link to
`/ui/inbox?kind=approval&item=<id>`, and a task completion notice links to
`/ui/runs/insights?task=<id>`.

## Data and live updates

- **TanStack Query** owns every read. Polling is calm by rule
  (`src/lib/polling.ts`, tasks #1786 and #1850): a poll stops while the tab is
  hidden and backs off exponentially (up to 5 min) while its reads fail, and a
  failed read is retried once, after 2 s. Reads are same-origin cookie
  `fetch`es to `/api`. Shared fetchers live in `packages/web/src/lib/api/` and
  are re-exported from `src/lib/hub.ts`; area-only fetchers live in
  `src/app/<area>/api.ts`.
- **Health.** `/ui/settings/health` polls `/api/health` once a minute and
  never on hub events. Its Refresh sends `?force=1`, which also bypasses the
  tool's own report cache and worktree survey, and never shares a hub call
  that began before it: forced requests that arrive before a queued call
  starts share it; every doctor call runs as its own task, so a closed
  request never fails the requests sharing it; and each request waits at most
  300 s, then gets the last reading with an error, or a `TimeoutError` (HTTP
  500) when there is no reading yet.
- **Inbox.** The Inbox polls `/api/attention` every 60 s
  (`src/lib/attention/useAttention.ts`) and `/api/approvals` every 15 s
  (`src/lib/approvals/useApprovals.ts`), and refreshes on `agent.approval.*`,
  `agent.task.*` and `agent.message.*` events.
- **Event stream.** The SSE route applies the verified identity's
  named-profile `event_log_query` ceiling before starting replay or a live
  reader (HTTP 403 when denied), including authenticated requests in warn
  mode. Unauthenticated local warn-mode clients keep the default developer
  read access. The app opens **one** `EventSource('/api/events/stream')` per
  tab (`src/lib/eventStream.ts`). Each page subscribes with the channels it
  cares about, filtered in the browser, so mounting a page never reopens the
  connection. A `cursor` frame marks the stream connected (opening frame and
  heartbeat); a `message` frame invalidates the matching queries, at most once
  per 5 s per page and never while the tab is hidden (one refetch is replayed
  when it is shown again). An `error` marks the stream reconnecting; the
  status bar shows the state. Page correctness never depends on event
  coverage: each query keeps its own refetch cadence.

### Event stream: resume and capacity (task #1811)

- **Frame ids.** Each `message` frame's SSE `id` is the event's ULID; the
  opening `cursor` frame's `id` is `seq:<n>`, its start seq. A browser
  `EventSource` therefore sends the last id it saw as `Last-Event-ID` when it
  reconnects.
- **Resume.** A valid `Last-Event-ID` sets the cursor and takes precedence
  over a `since` or `after_seq` query cursor: a ULID maps to `since`
  (translated to a `seq` once, at connect, with the same at-least-once rule as
  a `since` query) and `seq:<n>` maps to `after_seq`. A browser sends the
  header only when it reconnects the same `EventSource`, with the id of the
  last frame that stream delivered, so it is never older than the cursor in
  that stream's own URL (the app puts a resume cursor in the URL when it
  re-opens a refused stream). A header that is neither form is ignored when
  the query supplies a cursor and is `400` (like a bad `after_seq`) when it is
  the only cursor, so it never silently starts at the tail. A blank header is
  no cursor. `seq` is the event log's ROWID, so a `seq` cursor is not stable
  across a `VACUUM` (nothing in the hub runs one). Under `UA_APPROVAL_TOOLS`
  the cursor is part of the approval payload, so a reconnect from a new
  `Last-Event-ID` parks a new approval: a pass for one start does not cover a
  replay from another.
  Why one rule and no separate `resume` parameter: a query cursor says where
  a URL's *first* connect starts, while `Last-Event-ID` is the browser's own
  record of where *this* connection got to, so it is always the newer of the
  two and the header alone needs no new parameter, no new precedence table
  and no change for clients that send no header. Naming both `since` and
  `after_seq` in the query is still `400`.
- **Capacity.** The `UA_UI_MAX_STREAMS` slot is reserved before the response
  is returned. A full stream is HTTP `429`, `Retry-After: 30` and
  `{"error": "too many concurrent streams"}`; it holds no slot. A reserved
  slot is released when the stream ends, when the client disconnects, and
  when the response is dropped before it starts.
- **Browser behaviour.** An `EventSource` cannot read a status or a header,
  and a non-200 reply fails the connection permanently (`readyState` is
  `CLOSED`, no automatic retry, and the SSE `retry` field does not apply).
  The app's `openEventStream` (`src/lib/eventStreamTransport.ts`) therefore
  re-opens a `CLOSED` stream itself after 30 s, doubling per consecutive
  refusal to 5 min and resetting when a stream opens, passing the last event
  id it saw as `since` or `after_seq` (replacing any cursor already in the
  URL) so the gap is replayed. Ordinary drops stay with the browser.

### Identity and version discovery

`GET /api/whoami` returns the current `agent`, authentication `via`, session
`csrf`, authentication configuration, and control capability fields. It also
exposes the hub and API compatibility fields:

| Field | JSON type | Meaning |
|---|---|---|
| `hub_version` | string | Installed `unified-agent` Python distribution version (including editable installs); `"unknown"` when a source checkout has no installed package metadata. The status bar shows it. |
| `api_version` | integer | HTTP API contract version, currently `1`; independent of the package release version. |

[`API_VERSION`](../src/unified_agent/webui/contracts.py) increments for
breaking API field, type, or behavior changes. Additive fields and endpoints
keep the current API version.

Version discovery uses the same host, origin, and authentication guards as the
rest of `/api/whoami`: enforced authentication requires a valid identity and
returns `401` otherwise. Warn mode keeps the existing unauthenticated identity
behavior. Version fields do not expose bearer tokens or session cookies;
`csrf` continues to be the browser session's mutation token.

## Page contracts

The sections below describe the hub contracts behind specific pages. Each
names the v2 page that uses it.

### Approval detail (task #1263)

The Inbox (`/ui/inbox?kind=approval`) is the reviewer's primary screen. The
list from `GET /api/approvals` stays compact: it strips `payload` and `reason`
so a gated tool's raw arguments are not shown until a reviewer opens an item.

Opening an item fetches `GET /api/approvals/{id}`
(`api.approval_detail_payload`). That opt-in read adds `gate` (today always
the global allowlist), `device` plus `device_verified`, and `pr_url` /
`commit_sha` when the gated payload names one. Payload values still pass
through `sanitize_metadata`: a secret-shaped argument is elided before it
reaches the browser. `device_verified` is true only when
`registry.last_seen_via == "hub_request"` (an authenticated request the hub
itself observed). Heartbeat, event, executor-poll, and backfill hosts render
as unverified. Unknown ids return `{"error": ...}` mapped to HTTP 404.

The detail shows what will run (tool, arguments summary, requester) before
Approve is enabled. The hub requires a different human to approve than the
one who asked (`tools/approvals.py` `approval_resolve`); the Inbox says so
instead of offering Approve on the person's own request.

### Attention and digest

Attention feeds the Inbox and Home. `GET /api/attention` always includes
`needs_operator` (task #2411,
[`needs_operator.py`](../src/unified_agent/needs_operator.py)): one closed
list of what waits on the human operator. The Inbox lists it, and `ua needs`
prints the same payload. Each row has `kind`, `subject`, `title`, `since`,
and `act.pane` (`approvals`, `tasks`, `messages`, or `sessions`) with params
naming the place to act. Kinds are closed: a new source has to be listed in
`DECLARED` and given a producer, or `payload()` refuses to compose.

| Kind | What it is |
|---|---|
| `approval` | A pending approval |
| `operator_task` | An unfinished `operator-only` lane task |
| `red_tier_hold` | A task whose notes carry a red-tier review hold |
| `held_pr` | A task waiting on human-merge or a held review, excluding red-tier |
| `operator_reply` | Mail waiting on the operator's reply |
| `wake_grant` | An opted-in session wake that has not been granted |

A producer that fails to read is listed in `errors` and never reads as
"nothing waiting". Each kind is capped at 100 rows.

The same payload still returns the older actionable items sorted by severity
then age. Each of those has `kind`, `severity`, `source`, `reason`, and
`age_s`. Kinds include `approval`, `review_wait`, `awaiting_legacy_operator`,
`plan_approval_wait`, `human_merge_wait`, `blocked_task`, `stale_claim`,
`anomaly`, `identity_drift`, `stale_agent`, `stale_worktree`,
`executor_degraded`, `autonomous_cycle_stopped`, `evidence_gap` (an
`audit.evidence.gap` ledger event from the last seven days), and
`inconsistent_completion`. Expected-offline agents and acceptance-smoke cycle
cleanups are returned under `informational` and do not increment `total`.
`stale_worktree` rows are capped (overflow is informational; never
auto-deleted).

`GET /api/digest` groups the same items into waiting-on-the-operator, stuck,
running, owners, and changed-today; Home reads it. Missing sources are listed
in `omitted` so a clean scan reads as "nothing checked" rather than "nothing
found".

A `[waiting-for: review]` marker on a `done` task, or a task whose notes
already carry a terminal `[review: …]` line, is not paged again.

### Messages (task #1864)

`/ui/inbox/messages` shows who is talking to whom, what is waiting on a reply,
and lets the signed-in identity message any agent or one of its live sessions.
It sits over the messaging layer (`messaging/`): the per-recipient mailbox
index, its read and answered state, and the session registry.

**Conversation list.**
`GET /api/messages/conversations?filter=all|unread|waiting&participant=&limit=50`
(at most 100) returns conversations newest activity first. Each row has its
participant labels and session ids, `total`, `unread` (visible messages their
recipient has not read, as a label or through any session), `unread_mine`
(those addressed to the caller), `waiting` (`expects_reply` messages the
recipient has not answered) and a 160-character preview of the newest visible
message. `next_before_seq` (set when more exist) is the `before_seq` of the
next, older page; the page shows it behind "Load more conversations".

**Thread.**
`GET /api/messages/conversations/{conversation_id}?after_seq=|before_seq=&limit=200`
returns `message_list` rows oldest first (the newest page without a cursor;
with `before_seq`, the page before it, which the page loads behind "Load
earlier messages" while `has_older` is true, passing `oldest_seq`), with read
receipts from the recipient's sessions folded into `read_at` and `status`
(`delivered`, `read`, `answered`). A conversation with no message visible to
the caller is `404`, the same answer as one that does not exist.

**Composer.** `POST /api/messages` takes
`{act, body, message_id, to_agent?, to_session?, reply_to?, expects_reply?}`.
The composer offers `request`, `inform` and `query`; `done` and `handoff` stay
on the MCP tool. The live-session picker reads
`GET /api/messages/sessions?agent=` (`registry.list_live_sessions`: open
sessions seen within `UA_SESSION_LIVE_MINUTES`, no paths or transcripts).
Replying from a thread sets `reply_to` and the counterpart as recipient; the
hub routes a reply to the session that sent the message when that session is
still running.

| Rule | Behavior |
|---|---|
| Sender | Always the verified identity. A body naming `from_agent` or `from_session` is `400`. |
| Gates | The send calls the registered `message_send` tool, so its access, policy, approval and rate-limit gates run as for an MCP call. `202` when an approval gate parks it; a policy refusal is `403` and a rate limit `429`, each with `reason_code`, and the page says so instead of inviting a retry. |
| Idempotency | `message_id` is required (16 to 64 of `A-Z a-z 0-9 - _`). The composer mints one per composed message and reuses it when a failed send is retried; any edit mints a new one. A retry answers `200` with `duplicate: true`; a `message_id` another sender holds answers `409` without that sender's receipt. |
| Size | Body at most `UA_MESSAGE_MAX_BYTES` (4096 by default) UTF-8 bytes. The conversation list carries it as `max_body_bytes`, and the composer's counter and Send button follow it. Request bodies over max(64 KiB, 6 × that cap + 4 KiB), room for a fully JSON-escaped body, are `413`, refused while reading. |
| Rendering | Bodies are agent-written and untrusted. The page renders them as plain text only, never HTML or Markdown. |
| Read state | No route here marks mail read or acked; agents keep their own inbox state. |
| Refresh | Invalidation on `agent.message.*` stream events (coalesced, held while the tab is hidden), plus a slow fallback refresh while visible. No fast poll. |

**Who may read what.** The routes take the MCP ceilings: `message_list` for
the two reads, `message_send` for the send, `session_list` for the picker,
each checked against the request's profile with
`profiles.tool_is_allowed_for`. The verified human operator passes that
ceiling (the scopes are `operator=True`, the task #1865 rule: the operator
label on a hub credential, never the operator profile), so the operator login
uses the page although its profile holds no message tools over MCP. Scope
then follows `message_list` exactly: the operator login and the
chief-of-staff profile see every conversation; any other identity sees only
messages it sent or received, with mail addressed to one of its sessions
hidden as `message_list` hides it without a `session_id`. In a conversation
it shares with others, a scoped caller sees only its own messages. All four
routes need a verified identity, even in warn mode.

**Hand off to a task (task #1426).** The open thread has a *Hand off*
control: create a task (title, optional project) or link an existing task by
id. `POST /api/messages/conversations/{conversation_id}/handoff` takes
`{title, project?, priority?}` or `{task_id}` and answers 201 when it created
a task, 200 when it linked one or the same handoff repeats, and 404 for a
conversation the caller cannot see. The task write passes the same gates as a
direct `task_add` / `task_update` with the same arguments, so it answers 202
with an `approval_id` when `UA_APPROVAL_TOOLS` parks it (nothing is written;
hand off again once approved), and 403 or 429 when policy refuses it. Linking
runs `task_update` under the caller's own profile; the operator profile does
not expose it, so the operator gets 403 and creates a task instead. A
concurrent identical handoff answers 409 with `Retry-After`; its retry
returns the same task. The task gets one
`[conversation: <id>] handed off from the agent conversation` line and the
hub records an `agent.conversation.handoff` event; no message body is copied
into the task or anywhere else, so the mailbox thread stays the only
transcript. `GET .../handoffs` lists the tasks a visible thread went to,
oldest first. It reads the task line, not the event, so pruning the event log
or an event write that failed after the task write never hides a link; `by`
and `at` come from the event while it exists and are `null` after it is
pruned. The page refreshes the list live on `agent.conversation.handoff`. A
repeated create with the same title, project and priority returns the same
task; another project gets its own. The same actions are on MCP as
`coordination_manage` `conversation_handoff` and `conversation_handoffs`.
Starting or resuming the agent's live session is on the Sessions pages
(#1740/#1741); the composer here messages an agent or one of its sessions.

**Completion return (task #1993).** Creating a task from a thread records a
`task_origins` row (`surface=messages`). Linking an existing task records
nothing. When that created task reaches `done`, `blocked` or `cancelled`, a
short notice is posted back into the conversation: task id, status,
neutralised title, a link to `/ui/runs/insights?task=<id>` when a public
base URL is set, and the task's
`evidence_ref` when it is a commit SHA or an `https` URL (never note text). An
`https` URL on `github.com` or the `UA_PUBLIC_URL` host is a link; any other
`https` URL is shown as code, not a link. A ref the redactor would change is
dropped. The sender is the roster service label in
`UA_MESSAGES_REPORT_AGENT`. Unset or unknown: notices stay pending and expire
after 24 hours (`completion_return.DELIVERY_MAX_AGE_HOURS`). The notice is
addressed to the thread's requester, and a requester that is not a known
agent label (`config.is_known_agent_label`) gets no notice even with
`UA_MESSAGES_REPORT_AGENT` set: `completion_return._send_messages` marks the
row a terminal `skipped` (`skipped: operator recipient` when the requester is
`UA_OPERATOR_LABEL`, `skipped: recipient not on the roster` otherwise) and
never retries it. On a closed roster (`UA_AGENT_LABELS` set) that includes
the human operator's login, which is never a roster label. On an open roster
the operator label is addressable and the notice is delivered. Same contract
as Slack and Zulip completion return; see [configuration.md](configuration.md).

### Board (task #728)

`GET /api/board` is a read-only snapshot for the Work board
(`/ui/work?view=board`, which requests `include_cancelled=1`). It reuses the
same auth and origin guard as every other `/api` GET and never mutates tasks.

| Query | Effect |
|---|---|
| `project`, `lane`, `agent`, `priority` | Server-side filters |
| `min_age_days` (alias `age`) | Drop cards younger than N days |
| `swimlane_by=lane\|project` | Card swimlane key (UI grouping hint) |
| `include_cancelled=1` | Show cancelled cards (hidden by default) |

Columns are exactly the six task statuses (`open` is labeled Backlog; there is
no seventh column). Done is a rolling 30-day `completed_at` window. Cards
carry id, title, priority, claimed_by, age, worktree/branch, dependency
count, blocked reason, and aging/WIP alerts (`aging_open` >14d, `aging_doing`
>48h, `lock_expired`). Pending delegations appear as first-class cards with
`deadline_remaining_s`. WIP lane alerts come from `config.lane_wip_limits()`
(`UA_LANE_WIP`, task #705). The Work pages refresh on `agent.task.*` events.
A move between columns goes through `POST /api/action/task`; with the
keyboard it is a "Move to" menu that asks for confirmation.

### Task graph

`/ui/work?view=graph` draws the `task_graph` slice of `GET /api/overview`
(`task_graph_payload`): the filtered tasks with their parent and dependency
links, plus each task node's attribution and proof (`audit`). It replaces the
classic Task & Plan Graph view (`?view=task-graph` redirects here).
`&scope=work` switches to the work graph (`GET /api/work-graph`; see
[Work graph](work-graph.md)). Both render on `GraphCanvas` with labelled
edges (for example "blocks", "parent of", "serves"), a legend that explains
each label (`EDGE_LEGEND` in `src/app/work/model.ts`) and a table fallback.

### Flow metrics (task #729)

`GET /api/flow-metrics` is a read-only snapshot shown on `/ui/runs/insights`.
Same auth and origin guard as every other `/api` GET; never mutates tasks.

```json
{
  "cycle_time": {"p50_s": 1234.0, "p90_s": 5678.0},
  "lead_time":  {"p50_s": 2234.0, "p90_s": 6678.0},
  "wip_age": {
    "buckets": [{"label": "0-1d", "count": 3}, {"label": "30d+", "count": 1}],
    "oldest_s": 3456000.0
  },
  "window_days": 30,
  "as_of": "2026-08-31T12:00:00+00:00"
}
```

(The example is abbreviated: `buckets` always carries all six labels:
`0-1d`, `1-3d`, `3-7d`, `7-14d`, `14-30d`, `30d+`.)

- **cycle time** = `claimed_at` → `completed_at`; **lead time** =
  `created_at` → `completed_at`. Samples are done tasks inside the rolling
  30-day completion window; percentiles use the nearest-rank method. `null`
  p50/p90 means "no samples in the window", not zero, and the page says "Not
  enough data" rather than painting 0.
- **WIP age** buckets cover the active queue (`open`/`doing`/`waiting_review`/
  `blocked`) by `created_at` age; `oldest_s` is null when the queue is empty.
- `as_of` uses the same clock seam as `/api/board` so the two agree on "now".

This shape is **pinned**. Change it only with a note on task #729.

The same cron tick that runs the review pipeline also grooms the queue
(`unified-agent-review scan` → `queue_hygiene.run_hygiene_pass`, task #729):
stale claims (expired `lock_expires_at`) are released, `open` tasks older than
14 days are demoted one priority step (`low` stays), and `done` tasks older
than 30 days get `archived_at` stamped. Each action is marker-gated
(`[hygiene: <kind> @ ts]` in task notes) and notifies at most once: re-running
the pipeline over unchanged state is a no-op.

### Task lineage (task #730)

`GET /api/execution-graph?task=N` narrows the same endpoint to one task's
execution lineage. `/ui/runs/insights?task=<id>` draws it as a graph. Without
the `task` parameter the endpoint keeps returning the full-graph payload.
Same auth and origin guard as every other `/api` GET; read-only.

```json
{
  "task": {"id": 730, "title": "A3: lineage graph surfacing"},
  "lineage": {
    "delegations": [
      {"id": 12, "status": "in_flight", "from_agent": "agent-hermes",
       "to_agent": "agent-agy", "task_id": 730}
    ],
    "dispatch_runs": [{"id": "run-01J…", "task_id": 730, "status": "running"}],
    "autonomous_cycles": [{"id": "cy-01J…", "task_id": 730, "status": "pr"}],
    "refs": {"pr_url": "https://github.com/…/pull/1",
             "worktree_path": "~/…/.worktrees/task-730",
             "branch": "task/730-lineage"}
  },
  "empty": false
}
```

- One null-tolerant JOIN over existing rows only, no new tables.
  `delegations` exclude `cancelled`; `dispatch_runs` and `autonomous_cycles`
  left-join on `task_id`, so legacy rows with no lane, run, or PR render as
  empty nodes.
- `refs.pr_url` prefers a cycle's `pr_url` and falls back to a URL-shaped
  `tasks.evidence_ref`; `worktree_path`/`branch` come straight from the task
  row.
- Never 500: a blank, non-integer, or unknown task id returns `200` with
  `{"empty": true}` (unknown ids still echo `task.id` with a `null` title).
  The page shows "No recorded lineage" for an empty result; nothing is
  invented.
- The MCP surface for the same projection is the `coordination_manage` action
  `lineage` (`{"task_id": N}`): one lineage definition for the UI and agents.

This shape is **pinned**. Change it only with a note on task #730.

### Starting a session: dispatch contract

`/ui/sessions/new` (`src/app/sessions/StartSessionPage.tsx`) starts work in
three ways: a dispatch to an agent through the executor, the next ready task
of a dispatch stack, or a ua-node session on a device.

`POST /api/action/dispatch` requires an authenticated web session and the
CSRF header, then routes through the existing `delegate` tool so the executor
remains the only process that claims work and starts agent runs.

```json
{
  "agent": "agent-agy",
  "task_id": 252,
  "prompt": "Run the smoke check",
  "context": ["task_notes", "recent_validation"]
}
```

`agent` must be a configured label and may not equal the requesting identity.
`prompt` is required and capped at 1,000 characters. `task_id` is an optional
positive parent task. `context` is limited to `task_notes`,
`recent_validation`, and `related_pr`.

The `202` response contains `status`, `task_id`, `parent_task_id`,
`delegation_id`, `session_id`, `worktree`, and `pr`. The prompt and raw
executor logs are never returned. The initial state is `pending`; the
executor later creates the session and worktree records. This 202 is accepted
work, not an approval. Missing validation, artifact, review, or PR records
are represented as `null` in `GET /api/execution-graph`, not inferred.

**Dispatch stacks (task #754).** `GET /api/dispatch-stack` accepts a
comma-separated `task_ids` list and optional `project` and `expected_lane`
guards:

```text
/api/dispatch-stack?task_ids=812,813&project=unified-agent&expected_lane=default
```

The response topologically orders the selected tasks, exposes each
prerequisite, and returns one `next_task_id`. A task is eligible only when its
selected and external prerequisites are `done` or `cancelled`, its project
and lane guards match, and the existing coordinator says its pending
delegation is approved and ready. `POST /api/action/dispatch-stack` launches
only that next task; the coordinator rechecks the guards and approval
atomically before starting the process. A stack with no eligible increment
returns `409` and does not launch anything.

**Follow-up checklist (task #755).** When a durable dispatch run reaches
`finished`, the scheduler appends one idempotent `[follow-up: pending ...]`
marker to the task. It records the run ID, the evidence pointer when present,
and four bounded gates: `tests`, `ci`, `review`, and `live-health`. Duplicate
terminal reports reuse the existing marker, so completion retries do not
create duplicate follow-up work. The marker records required follow-up; it
does not automatically review, merge, or deploy.

**Launch settings (task #1742).** ACP starts also accept `permission_mode`
(default `supervised`) and advertised `model` IDs. The form uses the last
observed catalog and always renegotiates before work. Full access is an
explicit choice covered by the existing dispatch approval. Unmapped
Hermes/Cursor/Copilot/OpenCode adapters offer only a supervised request while
preserving their unclassified native default; the page never labels that
native permission policy supervised. Requested values are shown separately
from the actual native mode and effective model, with missing values marked
unreported. See
[the session settings contract](harness-control-matrix.md#session-models-and-permission-modes-task-1742).

### ua-node sessions: start, stream, cancel (task #1329)

The Sessions pages are the operator surface for work on a registered
**ua-node**: start on `/ui/sessions/new`, the transcript on
`/ui/sessions/:id` (`Transcript.tsx`), and queued device work on
`/ui/sessions?tab=work` (`DeviceWorkPanel.tsx`). This is not a PTY. The node
connects *out* to the hub; the browser never talks to the device.

**Inventory.** `GET /api/nodes` (`api.nodes_payload`) lists registered nodes
only; it does not invent a row that has not called `/api/node/register`. Each
row carries advertised `backends` (the launch options) and `worktree_roots`.
Installed harness metadata stays diagnostic; `node_queue._accepting_node`
rejects a backend the node did not advertise. `recent_items` is the last 25
`node_dispatch` rows.

**Start.** The form POSTs CSRF `{ action: "start", node, backend, cwd,
prompt, task_id? }` to `/api/action/node-session`. The route is the browser
counterpart of `POST /api/node/dispatch`: it runs
`node_queue.request_dispatch`, which checks shape and node advertisement
**before** the `node_dispatch` prod-action gate. Constraints from
`node_queue._validate_item`:

| Field | Constraint |
|---|---|
| `prompt` | Non-empty, ≤ 200 000 characters |
| `cwd` | Required, ≤ 4096 characters; the node also requires it inside a worktree root |
| `backend` | Hub vocabulary in `NODE_BACKENDS` (`codex`, `agy`, `pi`, `claude`, `cursor`, `hermes`, `copilot`, `devin`, `agy-interactive`, plus `acp:<harness>` from `acp_matrix.ACP_HARNESSES`) **and** advertised by that node |
| `timeout_s` | 1–21600 s (default 3600) |
| `task_id` | Optional integer (the form accepts a positive integer string) |
| HTTP | `201` queued; `202` `{ status: "approval_pending", approval_id, tool: "node_dispatch" }` until a human approves that exact payload; `401` without identity |

**Stream.** The transcript is `agent.node` events, not a raw ACP byte stream.
History is `GET /api/events?channels=agent.node&limit=200`; live updates come
from the shared event stream filtered to `agent.node` and the item's
`item_id`. ACP `session/update` notifications the node posts to
`/api/node/events` land on the same channel as evidence.

**Cancel and restart.** Same CSRF route: `{ action: "cancel", item_id }` sets
`cancel_requested` on `queued`/`claimed` rows (`node_queue.cancel`). A
**queued** item never reaches the node: the node's next poll marks it
`cancelled` instead of claiming it (`claim_next`). Only a **claimed** item's
cancellation is relayed to the node, on its next heartbeat.
`{ action: "restart", item_id }` re-queues a **terminal** item through
`request_dispatch` (so it is approval-gated again). Restart is refused (`409`)
while the item is live, and refused for `lease_expired_outcome_unknown`: a
lost heartbeat does not prove the backend stopped, so a second dispatch would
overlap. Reconcile the node outcome first
([cli-reference: ua-node](cli-reference.md#unified-agent-node)).

**Follow-ups: queue and steer (task #1740).** A live `acp:*` dispatch shows a
composer under the transcript (`Composer.tsx`). It POSTs CSRF
`{ item_id, mode, body, idempotency_key }` to `/api/action/node-followup`:

| Mode | Meaning |
|---|---|
| `queue` | Sent after the current turn ends, as the next `session/prompt` in the same ACP session. One follow-up per turn, in the order the hub accepted them. Allowed while the item is `queued` or `claimed`. |
| `steer` | Interrupt and redirect now. Emulated as `session/cancel` then `session/prompt` with every pending steer message; an agent that advertises `_session/steering` at initialize gets the message injected into the running turn instead. Allowed only once the item is `claimed`. |

The composer creates one `idempotency_key` per message and resends the same
key when the same message is sent again after a failure, so a retried click
never delivers twice: the hub answers `201` for a new follow-up and `200`
with `duplicate: true` for a key it already holds (the same key with a
different payload is `409`, and so is a key another identity already used or
is waiting on an approval for on that item). The route refuses a CLI executor
item, a finished or cancelling item and a node whose registered facts lack
`node_features: ["acp_followups"]` (`409`), and more than 20 undelivered
follow-ups per item (`429`). Its scope is stricter than `node-session`: the
caller's profile must expose `session_list` and `crew_view`, and the caller
must be the identity that started the item, the agent the session runs as,
or the verified human operator (`403` otherwise, or when another label holds
the item's task claim). Whether a follow-up needs its own approval depends on
who sent it (task #1872). A follow-up sent with the operator's credential
(the `UA_OPERATOR_LABEL` identity on a bearer, OAuth, cookie or WorkOS
channel, the same rule the routes use for operator recovery; never the
operator profile alone) is queued at once with `operator_authored: true` and
`approval: "not_required_operator"`, and the composer says it was sent with
the operator's credential and needs no approval. That records which
credential was used, not that a person typed the message. Any other
identity's follow-up passes the `node_dispatch` approval gate on its own
text: the first send answers `202`
`{ status: "approval_pending", approval_id, tool: "node_dispatch" }`, the
composer keeps the message and key and says so, and sending the same message
again after approval queues it (`approval: "gate_passed"`; `403` once
denied). Revocation and `UA_AGENT_SCOPES` (`dispatch`) apply to the operator
too: only the second approval is skipped. The composer takes the path from
the reply, never from a client-side guess. A follow-up runs under the
session's own permission relay, so steering grants nothing new. Button state
and hint text come from the node's `controls[backend]` row (`queue`,
`steer`). The transcript shows each follow-up as `follow-up queued · <mode>`,
then the node's `followup_delivered` event (with the `path` taken: `prompt`,
`cancel_reprompt` or `native_steer`) or `followup_dropped`. The
`follow-up queued` row carries the operator's whole text. Follow-ups still
undelivered when the item ends, by any path, are closed as dropped with a
`followup_dropped` event.

**Resume and adopt (task #1741).** Each recent item carries `resume`
`{available, mode, label, reason}` from `/api/nodes`. When `available`, the
session page offers Resume, which POSTs CSRF `{ item_id, prompt }` to
`/api/action/node-resume`; the label names the path (`ACP session/resume`,
`ACP session/load` or the harness `--resume` flag). Otherwise it says why.
`/ui/sessions?tab=devices` (`DevicesPanel.tsx`) lists
`GET /api/nodes/native-sessions` (native Claude Code and Codex sessions each
node discovered under its worktree roots; metadata only) with an Adopt dialog
that POSTs `{ node, harness, external_session_id, prompt, backend }` to
`/api/action/node-adopt`. Both routes are approval-gated node dispatches
(`201` queued, `202` approval pending) and require the `session_list` and
`crew_view` profile ceiling. Details:
[harness control matrix](harness-control-matrix.md#resume-and-adopt-task-1741-verified-2026-10-03).

**Turn lifecycle.** ACP events keep their raw `sessionUpdate` kind and carry a
`turn_id`. The node adds `turn_start` (`source`: `initial`, `queue` or
`steer`), `turn_end` (`stop_reason`, `usage`), `turn_aborted` (`reason`:
`steer`, `cancel`, `agent_cancel`, `timeout` or `error`) and one
`session_end`, which closes the mirrored `agent_sessions` row.

### Live crew watch and intervene (task #1025)

This is **not** a PTY or tmux bridge. The session page reads
`GET /api/sessions/{id}/crew` and refreshes it on `agent.message*` and
`agent.crew*` stream events.

**Watch.** The payload is a session snapshot plus recent typed messages for
conversation `crew:{session_id}` (and matching `task_id` / `delegation_id`
mailbox rows), capped at `limit` (default 50, clamped 1–200). `live` is true
only while session status is `starting`, `running`, or `waiting`. When the
session has a worktree directory, the same payload includes the tail of
`.ua/interventions.jsonl`.

**Intervene.** `POST /api/action/crew-intervene` is a CSRF mutation. The body
is `{ session_id, body, message_id? }`. `from_agent` is **not**
client-supplied: the route uses the authenticated web identity (the operator's
web login aliases to the operator label). Constraints, all enforced in
`src/unified_agent/crew.py`:

| Constraint | Behavior |
|---|---|
| Body | Non-empty, ≤ 2000 characters, redacted before send |
| Sender ≠ session agent | Self-intervene is refused |
| Delivery | Typed `message_send` (`act=request`) into conversation `crew:{id}` |
| Task note | Best-effort `[intervention @ <ts>] session=… from …:` append (first 500 chars); `worktree=False` so it does not mint a worktree |
| Worktree drop | Append-only `.ua/interventions.jsonl` when a worktree path exists **and** the message is not a duplicate `message_id` |
| Duplicate `message_id` | `accepted: true`, `duplicate: true`; no second worktree line |
| HTTP | `202` on accept; `404` unknown session; `400` validation; `401` if the mutation has no identity |

The executor can read the JSONL drop without polling the hub. The bounded MCP
tools `crew_view` and `crew_intervene` expose the same surface to agent
profiles, pinned by
`tests/test_profiles.py::test_crew_tools_profile_exposure_matrix` (task
#1151):

| Profile | `crew_view` | `crew_intervene` |
|---|---|---|
| `developer` | yes | yes |
| `chief-of-staff` | yes | yes |
| `steward` | yes | no |
| `operator` | yes | no |
| `research` | no | no |
| `public` | no | no |

`crew_intervene` stays with developer and chief-of-staff. The human operator
intervenes through the CSRF-gated `POST /api/action/crew-intervene` above,
which records the authenticated web identity; `crew_view` is read-only and
needs no such gate.

### Hosts: devices, acceptance and harness config

`/ui/agents/hosts` (`src/app/agents/HostsPage.tsx`) shows machines, ua-node
devices and their acceptance, a join command for a new device, and the
harness configuration bundle.

**Machines (task #798).** `GET /api/devices` returns
`{as_of, local_host, hosts[]}`. Each host row has an as-of time and a
staleness label. Doctor, executor and worktree fields stay `reported: false`
/ `doctor_freshness: "missing"` until a snapshot exists; MCP last-seen never
fills those in. Identity prefixes (`Desktop_*` → desktop, `Laptop_*` →
laptop, `server_*` → hub) are labeled `host_source: "mcp_identity"` and
supply last-seen only.

**Devices: acceptance and retire (tasks #2112, #2208).** The devices table
lists every registered ua-node from `GET /api/nodes` with its agent, last
heartbeat and stored
[device acceptance](fleet-console.md#device-acceptance-task-2112) state.
Retired rows (`retired_nodes`) follow, with who retired them, when and why.

| Control | Request | Who |
|---|---|---|
| Run acceptance | `POST /api/action/node-acceptance` `{"action": "start", "node"}` | The node's own agent or a lifecycle operator |
| Re-evaluate | `POST /api/action/node-acceptance` `{"action": "evaluate", "node"}`; offered once a run exists | Same |
| Retire | `POST /api/action/node-retire` `{"action": "retire", "node", "reason"}` | Lifecycle operators only |
| Unretire (retired rows) | `POST /api/action/node-retire` `{"action": "unretire", "node", "reason"}` | Lifecycle operators only |

| Attest (a pending run whose checks all passed) | `POST /api/action/node-acceptance` `{"action": "attest", "node", "run_id", "evidence_ref"}` (task #2299) | An independent lifecycle operator, never the node's own agent |

Retire and Unretire open a confirm dialog that needs a reason; Attest needs
the independent evidence (a link, a hash or a note). All are CSRF-gated. The
hub's refusal is shown in the dialog, which keeps what was typed: 403 for an
identity that may not act (or a node's own agent attesting), 409 when a retire
meets queued or claimed work or the run is no longer pending. A parked action
says it is waiting for approval; reopening it keeps the reason or evidence, so
the person confirms the same action again once it is approved. See
[Retire a node](fleet-console.md#retire-a-node-task-2181).

**Harness config (task #1330).** One versioned bundle on the hub is the
source of truth for skills, rules text, and MCP server lists
(`src/unified_agent/harness_config.py`). `ua-node` renders per-harness files
under `UA_HARNESS_CONFIG_ROOT` (default `~/.unified_agent/harness-config`) and
**never writes** a surface until a human approves its
`harness_config_apply` grant. The node then writes the file and acknowledges
with `action=applied`, which consumes the grant. The write comes first: if
the acknowledgement fails, the file is already changed and the grant stays
unconsumed, so the next sync that still receives the grant rewrites the same
content (same hash) and acknowledges again.

| Route | Role |
|---|---|
| `GET /api/harness-config` | Active bundle and pending proposals (`hub_status`). Always `proposal_mode: true`. |
| `POST /api/action/harness-config` | CSRF `{ action: "publish", bundle }`. The Hosts page's publish form. Duplicate `version` is `409`. |
| `GET\|POST /api/node/config` | Device plane. GET `?node=` (or POST `action=get`) returns rendered surfaces and apply grants. POST `action=report` parks per-surface approvals and a vault Inbox note; `action=applied` consumes a grant after the node writes (write first, then acknowledge; see above). Device-flow credential required unless `UA_NODE_ALLOW_BEARER=1`. |

Publish refuses bearer or token MCP auth (`mcp_server` must be `oauth` /
`oauth2` / `none`) so devices never re-grow hand-copied secrets. Caps:
512 KB bundle, 256 KB per file, 64 skills, 32 MCP servers, 32 proposals per
report. Default surfaces: `claude` → `CLAUDE.md`, `agents` → `AGENTS.md`,
`cursor` → `.cursorrules`, `codex` → `codex/config.toml`, `opencode` →
`opencode/opencode.json`. Approve the matching `harness_config_apply` item in
the Inbox; the node then writes only that surface. See
[configuration.md](configuration.md) (`UA_HARNESS_CONFIG_ROOT`,
`UA_HARNESS_CONFIG_SYNC_S`).

### Fleet restart and device sign-in revoke (tasks #799, #1321)

Both are on the Hosts page. **Restart executor** appears only on the row of
the hub's own machine (`local_host`); other rows say it runs only there. It
asks first, and a parked restart is shown as waiting, never as sent.
**Signed-in devices** lists the sign-ins below by device, with **Revoke
sign-in for <device>** for a bound device and **Revoke session** for each
session with no device binding; each asks first and is approval-gated.
**Approver passkeys** (shown when OAuth sign-in is on) lists the passkeys,
registers one for the approver on this device (the hub's creation options,
the browser's WebAuthn prompt, then the signed registration) and removes one
of this approver's own after a confirm. On a device that is not an
approver's, or through a loopback forward, it explains why and links to the
Hosts page on the hub's issuer origin.

`POST /api/action/fleet` is the only live fleet mutation. Body:

```json
{"action": "restart_service", "host": "<local hub hostname>", "unit": "unified-agent-executor.service"}
```

Constraints, enforced in
[`fleet_actions.py`](../src/unified_agent/fleet_actions.py):

- `action` must be `restart_service`. `reap_executor`, `release_worktree`,
  and `reload_registration` are reserved stubs and return a deny.
- `host` must be a registered fleet host **and** equal the hub's
  `current_source_host()`. Remote restart is not implemented.
- `unit` must be on the restart allowlist (hub, executor, zulip-worker:
  systemd names or the matching `com.unified-agent.*` launchd labels).
- The call goes through `safety.check_prod_action_gate` (`restart` scope and
  a one-shot approval). A pending approval returns `approval_pending` and
  does not restart anything.

A successful restart records `agent.fleet.control` and returns before and
after probe state plus `audit_event_id`.

`GET /api/devices` also carries `oauth: {devices[], session_count,
oauth_enabled}`: every live OAuth refresh-token session in the hub's durable
store, grouped by the tailnet node it was bound to at device sign-in
(`device_node`, `device_node_id`; `bound: false` for sessions with no device
binding, such as browser authorization-code clients). Each session carries
`session_id` (a 16-hex prefix of the token digest), `agent`, `client_id`,
`auth_method`, `issued_at`, `expires_at`. No token value is ever returned:
the store only holds SHA-256 digests.

`POST /api/action/oauth-revoke` with `{"device": "<node name or id>"}` or
`{"session_id": "<16 hex>"}` (exactly one) deletes those refresh tokens and
drops their live access tokens. It goes through
`safety.check_prod_action_gate` (`revoke` scope and a one-shot approval),
same as fleet restart; a pending approval returns `202 approval_pending` and
revokes nothing. The MCP equivalent is
`oauth_device_manage(action="list" | "revoke", device=…, session_id=…)`
(withheld from operator clients, out of the default profile).

Clients, refresh tokens, and access tokens all survive a hub restart (stored
as SHA-256 digests), so a signed-in device keeps working with no re-login and
no forced refresh. The `system_doctor` row `oauth_store` reports client,
session, and bound-device counts, and reads DEGRADED if any durable write
failed since start (those sign-ins would not survive a restart).

### Operating: profiles, sprints and run settings (task #1757)

`/ui/agents/teams?tab=operating` (`src/app/agents/OperatingPanel.tsx`).
Settings resolve org default, then office, then sprint, then run, and each
resolved value names the layer that set it.

- **Profiles and departments** (`GET /api/operating`): each profile's
  settings with what it extends applied, marked enforced (checked live) or
  recorded (frozen on each run), and which profile each office runs on.
  Profiles and bindings are policy: the page prepares a draft of the
  `operating_profiles` block, checks that it parses and copies it, for a
  policy revision. It writes no policy.
- **Sprints** (`GET /api/operating/sprints?status=`, filter in
  `?sprint_status=`): create a sprint (name, profile, office, overrides as
  JSON, dates; the form is a draft kept per signed-in person), change a
  sprint's status, profile and overrides with one Save, and link or unlink
  tasks (`POST /api/action/operating-sprint`). An edit starts from the stored
  values: a clean edit follows each refresh, and saving an edit over values
  someone changed on the hub since is refused until the person reloads them.
- **Run settings** (`?run_task=<id>`, `GET /api/operating/resolve?task_id=`):
  the resolved settings for the task's next run, and setting or clearing its
  run layer (`POST /api/action/operating-run`). `set` replaces the layer, so
  the form starts from what is stored; Clear asks first.

### Skills and team catalog (legacy Skills & Teams)

`/ui/agents/skills` and `/ui/agents/teams?tab=registry`
(`src/app/agents/SkillActions.tsx`), all through
`POST /api/action/skills-teams` with `{action, params}`. What the signed-in
profile may do comes from the read's `permissions`.

- **Compare a directory** (`?surface=<dir>`): the drift report of a skill
  directory against the registry (missing, out of date, not in the registry,
  not assigned, in sync, with hashes). The checked directory is also where
  Install writes.
- **Install** a published skill version, optionally replacing an existing
  `SKILL.md` only if its current SHA-256 matches.
- **Publish** a skill from a workspace path. The hub reviews it first; a
  review that blocks it answers 200 with `blocked`, and the page shows that as
  a refusal with the findings, never as published.
- **Install a team revision** (registry tab) restores a captured office's
  responsibility and membership, after a confirm.
- **Starter team templates** (registry tab): bind a template's roles to roster
  labels and harnesses, preview, then install; any change after the preview
  needs a new preview before Install is enabled.

### Policy revisions (task #1352)

`/ui/agents/org` (`src/app/agents/PolicyRevisions.tsx`) lists the revisions
of the hub's authority configuration (`GET /api/config-revisions?kind=`; the
kinds are policy, profile, approval_rule and org, chosen with `?rev_kind=`),
says when the live configuration has drifted from the latest revision, shows
the latest change as a diff, and rolls back to an earlier revision after a
confirm (`POST /api/action/config-revision` with `{kind, revision}`). A
rollback is approval-gated; a parked one changes nothing.

### Usage: quota and delivery SLO (tasks #1136, #1259)

`/ui/settings/usage` makes two independent reads. Lane availability comes
from `GET /api/quota` (metered / unknown / exhausted with evidence). Delivery
SLO comes from `GET /api/delivery_slo` (Epic C #1188 exit-test metrics over a
trailing window, default 7 days). Quota unavailability does not hide the SLO
section.

The four SLO metrics are computed in
[`delivery_slo.py`](../src/unified_agent/delivery_slo.py) from existing
`dispatch_runs` and `tasks` rows, with no second ledger:

| Metric | Meaning | Target |
|---|---|---|
| `dispatch_success_rate` | Share of *terminal* dispatch runs (`finished`/`failed`/`blocked`/`denied`/`cancelled`/`expired`) that finished. `retryable` and `refused` are excluded from both counts. | ≥ 0.90 (`ok` / `breach`) |
| `p90_cycle_time_hours` | p90 of `claimed_at` → `completed_at` (hours) for done tasks in the window. Missing `claimed_at` falls back to `created_at`; the fallback count is in `detail`. | none: `ok` when data exists |
| `unattributed_completions` | Count of done tasks in the window with empty `claimed_by`. | 0 (`ok` / `breach`) |
| `ci_green_rate` | Share of PR-head CI runs that were green. Requires `UA_CI_GREEN_RATE_PROVIDER=gh` plus `UA_CI_GREEN_RATE_REPO`. | none: `unavailable` when unset |

A missing metric is `status="unavailable"` with `value=null`, never treated
as zero. `vault_wiki_custodian` and doctor briefings also expose
`delivery_slo` and `delivery_slo_line` and say so instead of raising when the
read fails. See [configuration.md](configuration.md) for the CI-provider
knobs.

### Memory (tasks #801, #2469)

`/ui/knowledge/memory` reads `GET /api/memory`, a thin wrapper over
`memory_search` / `memory_list`. Empty `q`/`query` browses newest-first. A
non-empty query runs hybrid FTS and semantic search. Optional `tag` and
`limit` (default 50, max 200). One note opens with
`GET /api/memory/note?key=`. Unlike the classic pane, v2 writes: create,
edit (replace or append), delete and restore, promote, and feedback, through
`/api/action/memory/*`. Replacing another author's note needs the explicit
overwrite choice. Request and response shapes are in
[Control Center API](control-center-api.md#knowledge-webuicc_knowledgepy).

### Audit: evidence ledger (task #781)

`/ui/settings/audit` loads `GET /api/evidence`. Filters match the CLI:
`task_id`/`task`, `since`, `until`, `agent`, `project`, `pr`, `item`, `limit`
(default 50, max 500). A non-digit `task_id` is HTTP 400; it does not
silently widen to the unfiltered report. `UA_EDITION=core` returns HTTP 403.
`?format=pdf` and `csv` attach a download; the CLI stays JSON, Markdown and
text. See [Evidence report](evidence-report.md). The provenance graph query
is the sub-page `/ui/settings/audit/provenance`; see
[Provenance graph](provenance-graph.md).

### Run waterfall

`/ui/runs/telemetry` reads `GET /api/telemetry`. The payload has `waterfall`
(traces, spans, usage summary) and `exception_digest` (failures,
latency/cost anomalies, repeated failures, fallback storms). Each span
carries an `evidence_ref` (`ulid`, `channel`, `created_at`) into `event_log`.
The page does not display raw event payloads, prompts, or secrets.

Optional query parameters: `trace_id`, `task_id`, `limit` (capped at 500). An
empty store is expected until producers emit `agent.telemetry.*` events. See
[Architecture](architecture.md#telemetry) for the span contract and privacy
bounds.

When an exception row carries a `task_id`, the page shows **Escalate**. That
posts `/api/action/task` with `append_note` only: it writes
`[exception-escalated: <ulid>] <kind>: <message>` onto the existing task. It
does not create a task, approve work, or change status (task #778).

### LangGraph checkpoints (task #753)

`GET /api/langgraph/checkpoints` exposes the durable SQLite checkpoint store
(`checkpoints.sqlite3`) behind the LangGraph execution plane.
`/ui/runs/graphs` shows it. Same auth and origin guard as every other `/api`
GET; read-only.

Query parameters:

- `run_id` (optional): filter checkpoints by run ULID (matches
  `thread_id = ua-lg:{run_id}`).
- `limit` (optional, default 20, max 100): maximum number of checkpoint
  records.

```json
{
  "checkpoint_db": "/path/to/langgraph/checkpoints.sqlite3",
  "exists": true,
  "count": 2,
  "checkpoints": [
    {
      "thread_id": "ua-lg:01J6...",
      "checkpoint_ns": "",
      "checkpoint_id": "ckpt-002",
      "parent_checkpoint_id": "ckpt-001",
      "type": "checkpoint"
    }
  ]
}
```

When no checkpoint database exists, the route returns
`{"exists": false, "count": 0, "checkpoints": []}` with `200` (never 404 or
500).

### Executor and delegations

`/ui/runs/executor` reads `GET /api/executor-delegation`
(`executor_delegation_payload`): executor health, the delegation tables,
recent failures, and stale worktrees.

- **Executor health** comes from `system_doctor`'s `active_executor_service`
  check. On Darwin that probe is `launchctl print` of
  `com.unified-agent.executor-agy`, not `systemctl --user show
  unified-agent-executor.service`. A Mac hub with a live Agy LaunchAgent
  reports LIVE. Codex has its own LaunchAgent
  (`com.unified-agent.executor-codex`) and is not mapped into this check.
- **Worktree census** calls `survey_worktrees()`, which reads
  `config.process_workspace_root()` plus `UA_AUDIT_ROOTS`. The operator login
  is `UA_AGENT_WORKSPACES` DENY: agent-facing `fs_*` / `run_cmd` still raise,
  but this read-only page must not. A survey exception is returned as
  `worktree_errors` / `worktree_error`. The page shows that on the worktree
  table only; delegation rows keep rendering.
- **Removal** stays pinned to `cleanup_stale_worktrees`, which still honors
  `workspace_root()` and therefore DENY. The page surveys. It does not
  delete.

### Planning canvas (task #807)

`/ui/work/canvas` hosts Excalidraw. `@excalidraw/excalidraw` 0.18.1 is an npm
dependency of `packages/web`, chunked into `dist/` by Vite and lazy-loaded
only when the page first opens; upgrades are ordinary dependency bumps. The
scene is stored verbatim per board id in `graph_canvas_scenes` via
`GET`/`PUT /api/graph-canvas`, the same storage the `graph_canvas_read` /
`graph_canvas_write` MCP tools use, so the browser and the agents see one
canvas. Sketches produce suggestions only: `graph_canvas_read` parses text
labels into task and dependency suggestions; nothing is created without human
approval through the existing task and approval flow.

**No control carries a hand-written option list.** Agents, projects, parent
tasks and workflows are all populated from hub reads. This is a rule rather
than a preference: a classic workflow picker shipped with workflow names
hard-coded in the markup, none of which had a definition in
`src/unified_agent/workflows/`, so every run failed with a 400 that the button
swallowed.

## Accessibility

The Control Center is a long-lived operator surface, so accessibility is
behaviour, not polish.

**The gate.** `packages/web/tests/**/*.a11y.spec.ts` run under Playwright and
`@axe-core/playwright` with tags `wcag2a wcag2aa wcag21a wcag21aa wcag22aa`
(`npm run test:a11y`, shared fixture `tests/a11y.fixture.ts`). The CI `web`
job builds protocol, client and web, typechecks, runs Vitest, builds,
installs Chromium, then runs the a11y gate. Any axe violation fails.

- `tests/certification.a11y.spec.ts`: each of the eight destinations in
  light, dark and high contrast shows exactly the eight destinations in the
  sidebar, marks its own as current and has no violation; every §3 route
  opens its page under its destination; every classic `?view=` id and alias
  opens its new page.
- `tests/app/shell.a11y.spec.ts`: every route, in three themes, at 1440 px
  and 390 px wide.
- `tests/app/<area>.a11y.spec.ts`: each area's pages and states.
- `tests/ui.a11y.spec.ts`: the design-system primitives on
  `/ui/?showcase=primitives`.

**The contract.**

- **Contrast.** `packages/web/src/index.css` holds the theme tokens (dark,
  light, and high contrast). `tests/design-tokens-contrast.test.ts` reads that
  file and fails when a text pair drops under 4.5:1 or a non-text boundary
  under 3:1.
- **Keyboard.** A global `:focus-visible` ring, a skip link, focusable scroll
  containers, keyboard alternatives to every drag (the board's "Move to"
  menu), and graph nodes on `GraphCanvas` with keyboard focus and a table
  fallback for screen readers. Spec §7 requires every interactive flow to
  have an e2e test that drives it with the keyboard at least once.
- **Announcements.** Toasts and write results are `role="status"`; errors
  stay until dismissed. Status dots are `aria-hidden`; the adjacent text
  always names the same state.
- **Data states.** Every query-backed region shows exactly one of loading
  (`Skeleton`), error (`ErrorState` with Retry), empty (`EmptyState`) or
  ready. A count is shown only from a successful, complete read; otherwise
  "–". An empty region and a broken region never look the same.
- **Motion.** `prefers-reduced-motion` disables transitions and smooth
  scrolling.
- **Names.** Every table has a caption, every header cell a `scope`, every
  form control a label (`Field`).

## Source layout and tests

```
packages/web/src/
  main.tsx                    entry: return path, legacy redirect, showcase, App
  index.css                   theme tokens (--ua-*)
  app/
    App.tsx router.tsx routes.ts EntryBoundary.tsx
    shell/                    sidebar, top bar, palette, shortcuts, status bar
    lib/                      v2-only helpers: mutations, epoch, drafts,
                              parkedWrites, legacyRedirect, returnTo, urlState
    home/ inbox/ work/ agents/ sessions/ knowledge/ runs/ settings/
    integrations/             mounted under /settings/integrations
  lib/
    ui/                       design-system primitives (Button, Dialog, DataTable, ...)
    api/                      shared /api fetchers, re-exported by hub.ts
    hub.ts polling.ts eventStream.ts eventStreamTransport.ts
    auth/SignInView.tsx  theme/  canvas/  approvals/  attention/  messages/ ...
```

Each area owns its directory and exports `routes` (and optional palette
`commands`) from `routes.tsx`; `router.tsx` loads an area on first visit.
Area-only fetchers live in `src/app/<area>/api.ts`.

Tests:

| Path | What it covers |
|---|---|
| `tests/app/<area>/*.test.tsx` | Vitest unit and component tests per area (jsdom) |
| `tests/app/<area>.e2e.spec.ts` | Playwright flows per area, hub mocked with `page.route` |
| `tests/app/<area>.a11y.spec.ts` | axe per area |
| `tests/app/lib/` | Write helper, owner epoch, owner switch, drafts, parked writes, legacy redirect |
| `tests/app/architecture.test.ts` | Forbids Web Storage, direct toasts and stray `QueryClient`s outside the epoch |
| `tests/app/support/` | Render helper, hub mocks, fixtures |
| `tests/nav.test.ts` | The eight destinations, every §3 route and every §11 id and alias, checked against the spec text |
| `tests/certification.a11y.spec.ts` | Eight destinations × three themes, every §3 route, every §11 id |
| `tests/app/shell.a11y.spec.ts` | Every route, three themes, 1440 px and 390 px |

Checks before a push, from `packages/web`: `npm run typecheck`, `npm test`,
`npm run build`, then `npx playwright test tests/app/<area>` for the area you
changed.

## Architecture

```
browser ──> /ui         React SPA (packages/web/dist), Vite base=/ui/;
                        503 when the build is absent (single mount, #993)
        └─> /api/*  JSON ──> server.REGISTERED_TOOLS ──> the same wrapped tool
                                                          callables MCP clients hit
        └─> /api/events/stream  SSE ──> events.query_events() seq (ROWID) cursor poll
```

Routes are appended to the hub's Starlette app inside
`server.wrapped_http_app`, *before* `HubAuthASGIMiddleware` wraps it, so `/ui`
and `/api` sit behind the same identity plane as `/mcp`.

**The browser never speaks MCP.** `server.py`'s `_http_settings()` enables
DNS-rebinding protection whenever `UA_HTTP_ALLOWED_HOSTS` is set, and a page
served by the hub sends a real `Origin`. `allowed_origins` used to be
hardcoded to `["null"]` (which is why `deploy/metrics.html` only worked from
`file://`). The default is now the hub's own origin and does **not** include
`null`; sandboxed iframes stay blocked unless `UA_HTTP_ALLOWED_ORIGINS` lists
`null` explicitly. `/ui` and `/api` also reject a `Host` that is not loopback
or `UA_HTTP_ALLOWED_HOSTS`. The UI talks to `/api` regardless, which also
removes the MCP handshake, session-id juggling and hand-rolled SSE parsing
from the client.

### Why `/api` dispatches through `REGISTERED_TOOLS`

So a UI request traverses the *identical* policy gate
(`safety.check_policy_gate`) and perf instrumentation (`monitor.instrumented`)
as an MCP call. The TUI local dispatch path does the same:
`tui._local_tool_registry()` is derived from `server.REGISTERED_TOOLS`
filtered by `profiles.allowed_tools` (the MCP `tools/list` filter), so it
cannot drift from the authorized surface.

**Known cost:** UI page loads write `agent_perf_samples` rows and therefore
appear in `perf_summary`. This is deliberate (policy consistency is worth
more than a clean perf table), and the calm polling keeps the volume in the
same order as one active agent. Do not investigate it as an anomaly.

### Why the event stream polls a cursor instead of subscribing

`events.subscribe()` is lower latency and already written. It is not used
because no `EventBusServer` had ever run in this deployment: `publish()`
returned `False` for every event, so a socket-backed stream would have shipped
an *empty feed* while passing a naive "does it emit?" test.
`events.query_events()` reads the durable `event_log`, which is a superset: it
sees events from every process, survives the bus being down, and its ULID
cursor is exclusive and monotonic so nothing is missed or replayed.

The bus is now embedded in the hub process
(`server._start_embedded_event_bus`), so socket fan-out works; the UI simply
does not depend on it. The transport is swappable at `sse._event_source()` if
that changes.

Both UI rewrites (the React app in 2026-09 and v2 in 2026-11) kept these
invariants: the cookie session model, the `/api` contracts, the SSE cursor
behaviour, and offline and local operation.

## Route inventory

Locked by `tests/test_webui_route_inventory.py` (`EXPECTED_ROUTES`: 231
unique paths; 236 `Route` objects, because some paths are registered once per
method). That test is the complete list; the tables below show the main
routes. The routes added for Control Center v2 (memory and vault writes, plan
and goal actions, routines, agent enrolment, connectors, keyed writes) are
documented in [Control Center API](control-center-api.md). `system_doctor`'s
`web_ui` check reports the live `web_routes()` count when LIVE. OAuth routes
(`/oauth/*`, `/authorize`, …) are a separate table (`oauth.http_routes()`),
mounted only when `UA_MCP_OAUTH=1`.

### Shell and session

| Method | Path | Role |
|---|---|---|
| GET | `/healthz` | Liveness (content-free) |
| GET | `/readyz` | Readiness: live-store write+read and WAL. 503 when the store is missing or read-only. `workos` is advisory |
| GET | `/ui` | App shell; 503 when the build is absent |
| GET | `/ui/{path:path}` | Assets and the extensionless client-route fallback; retired `/ui/legacy*` and `/ui/excalidraw/*` prefixes 404 |
| POST | `/api/session` | Token → HttpOnly cookie |
| GET | `/api/session/options` | Public. `{"workos": true}` when AuthKit sign-in is configured; discloses nothing else (task #2114) |
| GET | `/api/session/workos/login` | Public. Redirect to AuthKit (one organization, signed state + PKCE) |
| GET | `/api/session/workos/callback` | Public. Verify membership and mint `ua_ui`; redirect to `/ui` |
| DELETE | `/api/session` | Logout |
| GET | `/api/whoami` | Identity, `csrf`, `control_enabled`, `hub_version`, `api_version` |
| POST | `/api/slack/events` | Signed Slack ingress. Slack HMAC replaces hub bearer auth. No session cookie, no CSRF. See [Slack coordination](slack-coordination.md). |

### Read APIs

| Method | Path | Role |
|---|---|---|
| GET | `/api/overview` | Aggregated overview read |
| GET | `/api/attention` | Needs-operator and stuck items |
| GET | `/api/digest` | Decision-grade exception digest |
| GET | `/api/agents` | Agent registry list |
| GET | `/api/agents/{label}` | Single agent detail |
| GET | `/api/sessions` | Session list |
| GET | `/api/sessions/{id}` | Single session |
| GET | `/api/runs` | Uniform run rows across dispatch, self-pickup and session stores (task #1652). `?kind=`, `?agent=`, `?task_id=`, `?limit=` (default 100, max 500), `?cursor=` (the previous page's `next_cursor`; `(started_at, run_id)` descending). Read-only: lapsed leases and stale heartbeats read `abandoned` without a write. |
| GET | `/api/runs/{run_id}` | One uniform run row; 404 on unknown or other-tenant id. |
| GET | `/api/sessions/{id}/crew` | Live crew view: session snapshot, `crew:{id}` messages, worktree JSONL tail (task #1025). `?limit=` default 50, max 200. |
| GET | `/api/tasks` | Compact task list |
| GET | `/api/board` | Read-only board snapshot |
| GET | `/api/flow-metrics` | Cycle/lead percentiles and WIP age buckets (task #729) |
| GET | `/api/queue-health` | Desktop / review / laptop / archived buckets |
| GET | `/api/projects` | Projects |
| GET | `/api/review-approval` | Review waits and pending tool approvals |
| GET | `/api/executor-delegation` | Executor health, delegations, stale worktrees. Worktree survey errors stay in `worktree_errors` and do not fail the delegations tables. |
| GET | `/api/approvals` | Pending, live unconsumed passes, and inert `expired_passes` (task #596). Expired rows stay visible but do not authorize a retry; the newest 50 are listed, with `expired_passes_total` and `expired_passes_truncated` (task #1786). Compact: no `payload` / `reason`. Each row includes `run_id` (task #1653), the caller's current run or null. Rows are read in one batch; the response carries an `ETag`, and a matching `If-None-Match` gets `304` (task #1786). |
| GET | `/api/approvals/{id}` | Approval detail (task #1263). Sanitized payload; `device_verified` only for `hub_request`. Includes `run_id` (task #1653). 404 on unknown id. |
| GET | `/api/plans` | Plans list |
| GET | `/api/plans/{plan_id}` | Single plan and review gate |
| GET | `/api/proposals` | Session-learning and skill-cluster proposals |
| GET | `/api/org` | Office registry, members, live presence, owned work |
| GET | `/api/vault/search` | Read-only vault search |
| GET | `/api/vault/page` | Single vault page |
| GET | `/api/vault/ingest-status` | Inbox ingest hash status |
| GET | `/api/vault/briefing` | Vault briefing |
| GET | `/api/connectors` | Horizon-2 connector lifecycle |
| GET | `/api/quota` | Fleet lane availability (metered / unknown / exhausted) |
| GET | `/api/delivery_slo` | Epic C Delivery SLO metrics (task #1259). `?window_days=` defaults to 7. |
| GET | `/api/credential_pool` | Provider credential metadata, never a secret value |
| GET | `/api/slack_gateway` | Slack ingress status (LIVE / DORMANT / MISCONFIGURED) |
| GET | `/api/notify_delivery` | Outbound notify/iMessage outcomes |
| GET | `/api/dispatch_receipts` | Review/merge/dispatch delivery-receipt summaries |
| GET | `/api/cloud` | Private GCP worker health, backups, budget |
| GET | `/api/platform/manifest` | Object-model manifest |
| GET | `/api/platform/provenance-graph` | Provenance graph query |
| GET | `/api/capability-catalog` | Evidence-backed capability catalog |
| GET | `/api/memory` | Memory browse and search (task #801) |
| GET | `/api/agent-graph` | Topology nodes and edges |
| GET | `/api/execution-graph` | Run, validation and PR evidence; `?task=` for one task's lineage |
| GET | `/api/dispatch-stack` | Topological preview of a selected dispatch stack (task #754) |
| GET | `/api/cycles` | Autonomous cycle list |
| GET | `/api/cycles/{cycle_id}` | Single cycle |
| GET | `/api/health` | Doctor-shaped health payload |
| GET | `/api/zulip` | Zulip worker health and inbound |
| GET | `/api/devices` | Fleet host rows: doctor snapshots and MCP last-seen (task #798) |
| GET | `/api/nodes` | Registered ua-node devices and recent `node_dispatch` items (task #1329). Never invents an unregistered node. Each node carries its stored `acceptance` summary (task #2112); retired rows are left out of `nodes` and listed under `retired_nodes` (task #2208). |
| GET | `/api/harness-config` | Active harness-config bundle and pending proposals (task #1330). Read-only; publish is the mutation below. |
| GET | `/api/mobile/v1/projection` | Mobile/Watch projection |
| GET | `/api/langgraph/checkpoints` | Durable LangGraph checkpoint list (task #753) |
| GET | `/api/evidence` | 5-link attributable evidence report (enterprise; 403 in core). `format=json` (default), `markdown`/`md`, `text`, `csv`, or `pdf` (task #956). |
| GET | `/api/perf` | Perf samples summary |
| GET | `/api/events` | Recent event_log page (non-SSE). `since` (ULID) or `after_seq` cursor |
| GET | `/api/events/stream` | SSE activity feed. Every frame carries `seq`; resume with `after_seq` (`since` still accepted) or the `Last-Event-ID` header (which takes precedence). A full stream is `429` + `Retry-After` ([details](#event-stream-resume-and-capacity-task-1811)) |
| GET | `/api/ledger/rows` | Raw event_log rows in commit order: `after_seq`, `limit` (max 1000). `payload_raw` is the stored payload string, so a verifier can recompute `entry_hash` (task #1640). On this route, `/api/events` and `/api/events/stream`, `after_seq` must be an ASCII integer from 0 to 2⁶³−1 (empty means no cursor); anything else gets `400` (task #1683) |
| GET | `/api/telemetry` | Run waterfall and exception digest |
| GET | `/api/graph-canvas` | Excalidraw planning canvas scene (task #807) |

### Mutation APIs (session cookie + CSRF)

| Method | Path | Role |
|---|---|---|
| POST | `/api/action/approval` | Approve / deny |
| POST | `/api/action/plan` | Plan review-gate mutation |
| POST | `/api/action/proposal` | Accept / reject a learning or skill-cluster proposal |
| POST | `/api/action/task` | Claim / complete / update |
| POST | `/api/action/workflow` | Run a curated workflow |
| POST | `/api/action/dispatch` | Bounded agent dispatch |
| POST | `/api/action/dispatch-stack` | Launch the next eligible stack increment |
| POST | `/api/action/dependency` | Task dependency edge |
| POST | `/api/action/handoff` | Agent handoff |
| POST | `/api/action/crew-intervene` | Typed intervention into a live session (task #1025). Identity is the cookie session, not a client `from_agent`. `202` on accept. |
| POST | `/api/action/node-session` | CSRF start / cancel / restart for ua-node work (task #1329). Start and restart are the `node_dispatch` prod action (`201` queued, `202` approval pending). Cancel is attributed and not itself a prod-action gate. |
| POST | `/api/action/node-followup` | CSRF queue or steer for a live ACP node session (task #1740). `201` queued (at once when sent with the operator's credential, task #1872), `202` approval pending (any other identity), `200` `duplicate: true` for a repeated `idempotency_key`, `409` when another identity already holds that key on the item; profile must expose `session_list` and `crew_view`. |
| POST | `/api/action/node-resume` | CSRF resume of a finished node session with a new message (task #1741): ACP `session/resume`/`session/load` or the native resume flag. `201` queued, `202` approval pending, `409` when the agent or node cannot resume; `session_list` + `crew_view` ceiling. |
| POST | `/api/action/node-adopt` | CSRF adopt of a discovered native Claude Code / Codex session under the caller's identity (task #1741), idempotent per session; another label's session is `403`. `201`/`202`, `200` `duplicate: true` while its item is open. |
| POST | `/api/action/node-acceptance` | Start, evaluate or attest a device acceptance run (tasks #2112, #2299) |
| POST | `/api/action/node-retire` | Retire or unretire a device with a reason (task #2208) |
| POST | `/api/action/harness-config` | CSRF `{ action: "publish", bundle }` (task #1330). Apply stays an approval (`harness_config_apply`). |
| POST | `/api/action/cycle` | Cycle start / transition / kill |
| POST | `/api/action/fleet` | Approval-gated local service restart (task #799) |
| POST | `/api/action/oauth-revoke` | Approval-gated revoke of one device's OAuth sign-in (task #1321) |
| POST | `/api/action/session-credential-revoke` | Operator-only CSRF revoke of session-scoped hub MCP credentials (task #1744) by exactly one of `credential_id`, `item_id` or `node`; returns the revoked ids. See [cli-reference](cli-reference.md#session-scoped-hub-mcp-for-acp-sessions-task-1744) |
| GET, POST | `/api/oauth/passkeys` | List / register approver passkeys for verified-human OAuth consent (task #1322). Writes need a human identity on the approver's tailnet node plus CSRF; see [agent access](agent-access.md#who-may-approve-a-sign-in-verified-human-consent) |
| POST | `/api/oauth/passkeys/options` | WebAuthn creation options for that registration (same gate) |
| POST | `/api/oauth/passkeys/delete` | Remove one approver passkey (same gate) |
| PUT | `/api/graph-canvas` | Save Excalidraw planning canvas scene (task #807) |
| POST | `/api/messages` | Send one typed message as the signed-in identity (task #1864); idempotent on `message_id`. See [Messages](#messages-task-1864) |

`POST /api/session-capture` is a script ingest, not a browser mutation:
bearer token, no CSRF. See
[Memory, tasks, and vault](memory-tasks-vault.md#passive-session-capture-task-813).

`POST /api/llm/v1/chat/completions` is an opt-in model broker (J7 #1326),
not a browser mutation. It accepts a device-bound hub OAuth token only, uses
a hub-managed provider key, and records agent and device usage without prompt
or response content. See [LLM provider broker](llm-provider-broker.md).

`GET /api/whoami` returns `control_enabled: true` when mutations are mounted.
CSRF is not boot-only: the app refreshes the token from `/api/whoami` on a
CSRF 403 (one retry).

## Agent registry

`agents` and `agent_sessions` (schema v14, additive; no table was rebuilt).

Status is **derived at read time** by `registry.derive_status()`, never
stored: a stored status is written by the process whose death you are trying
to detect, so it reads "online" forever exactly when it matters.

| Status | Meaning |
|---|---|
| `online` | seen within `UA_AGENT_STATUS_THRESHOLDS[0]` (120s) |
| `idle` | within 900s |
| `stale` | within 24h |
| `offline` | older |
| `unknown` | never observed: a configured label with no activity, which is what config drift looks like |

Heartbeat writers, in descending order of signal quality:

| Writer | Via | Notes |
|---|---|---|
| Auth middleware | `hub_request` | The verified bearer identity: a cryptographic fact, not an inference |
| `storage.record_agent_event` | `agent_event` | About 18 existing call sites, no edits to any of them |
| `ExecutorDaemon.poll_once` | `executor_poll` | The only agent with no reason to touch the hub while idle |
| `agent_heartbeat` tool | `heartbeat_tool` | For bursty agents; the hook point for Claude Code SessionStart/Stop |

All are throttled per label by `UA_HEARTBEAT_MIN_INTERVAL_S` (15s) and can
never raise: same contract as `record_agent_event`.

**Unregistered labels are recorded, not rejected.** Live data contains
labels such as `unified-agent-cron`; `record_agent_event` has never validated
labels and many rows depend on that, so adding validation now would convert
a cosmetic problem into dropped telemetry. They get `known=0`, an
"unregistered" badge, and the `agent_label_drift` doctor check.

Sessions are opened around each Pi run by the executor, linking `task_id`,
`worktree_path`, and the `.worktrees/sessions/*.jsonl` transcript. A
`running` session with no heartbeat for `UA_SESSION_ABANDON_MINUTES` (30) is
reaped to `abandoned`: a row asserting something false is worse than no row.

## Auth

Two credentials, one identity plane.

- **Bearer token**: the hub's existing mechanism. Works everywhere including
  `/mcp`.
- **Cookie session**: `POST /api/session` trades a token for an HttpOnly,
  `SameSite=Strict` cookie (12h). Scoped to `/ui` and `/api` only. The same
  cookie is minted by **Sign in with WorkOS** when the hub's WorkOS leg is
  configured; the sealed WorkOS session stays on the hub
  (`ui_session_workos`) and never falls back to a plain cookie if a live
  membership check fails.

### Typed write errors (#2056)

`/api` writes that raise a typed tool refusal answer
`{"error": "<message>", "code": "<code>"}` (`_json_body_error`), matching MCP
`structuredContent`
([tools-reference.md](tools-reference.md#error-contract-typed-codes-2054)).
A parked approval is not a failure: it is always a **202** whose body names
`"code": "approval_required"`. Its other keys depend on which code path built
it, so branch on `code`, not on whether `error` is present.

- **Shared helper, no `error`.**
  [`_approval_pending`](../src/unified_agent/webui/routes.py) answers
  `{"approval_id": ..., "status": "pending", "code": "approval_required"}`.
  [`_json_body_error`](../src/unified_agent/webui/routes.py) and
  [`_write_error`](../src/unified_agent/webui/routes.py) hand any typed
  `ApprovalPendingError` to it, whatever the route's default status.
- **Dedicated route branches, no `error`.** Routes in
  [`routes.py`](../src/unified_agent/webui/routes.py) with their own approval
  branch (for example `routine_fire_http`, `action_node_session` and
  `node_dispatch`) return `approval_id`, `status` (`"pending"` or
  `"approval_pending"`) and `code`, some with richer pending fields (`tool`,
  `payload`).
- **Approval bodies that also carry `error`** (the `ApprovalPendingError`
  message):
  - `POST /api/action/fleet`
    ([`action_fleet`](../src/unified_agent/webui/routes.py)) and
    `POST /api/action/oauth-revoke`
    ([`action_oauth_revoke`](../src/unified_agent/webui/routes.py)):
    `status: "approval_pending"`, `approval_id`, `code` and `error`, capped at
    300 characters.
  - `POST /api/action/config-revision`
    ([`api.config_revision_rollback_payload`](../src/unified_agent/webui/api.py)),
    `POST /api/action/operating-sprint` and `POST /api/action/operating-run`
    ([`api.operating_action_payload`](../src/unified_agent/webui/api.py)), and
    `GET /api/skills-teams` and `POST /api/action/skills-teams`
    ([`api.skills_teams_payload`](../src/unified_agent/webui/api.py)):
    `pending: true`, `approval_id`, `code`, `error`, and `status: 202` (the
    number, not the string `"pending"`).
  - `POST /api/action/team-up`
    ([`api.team_up_payload`](../src/unified_agent/webui/api.py)):
    `pending: true`, `approval_id`, `code`, `tool` and `error`, with no
    `status` key.
- **A returned pause dict, no `error`.** When `team_spec.boot` returns its
  autonomy-review pause instead of raising, `POST /api/action/team-up`
  answers that dict (`booted: false`, `autonomy_review`, `reason`, `plan`)
  with `pending: true`, and the `_json` chokepoint adds the `code`. It has no
  top-level `approval_id` or `status`; a global approval opened for it is
  `autonomy_review.global_approval_id`.

| `code` | HTTP | Body |
|---|---|---|
| `conflict` | Always **409** | `error`, `code` |
| `approval_required` | Always **202** | `code`, plus the keys of the path above that built it; `error` only on the routes listed there |
| other typed codes | The route's own status | `error`, `code` |
| untyped | The route's own status | `error` only, no `code` key |

A 202 for accepted work (a dispatch whose agent has not started reports
`status: "pending"`) is **not** an approval and has no `code`. The app's
write helper follows exactly this rule (see [Writes](#writes)).
`POST /api/approvals/request` opts out: its 202 is a successful request, not
a refusal.

**The cookie never authenticates `/mcp`.** Tool calls stay bearer-only, so a
cross-site request riding the cookie cannot reach one. A static page cannot
hold a bearer safely: anything JS can read, an XSS can exfiltrate, and what
would leak is a permanent tailnet-wide MCP credential.

`Secure` is set only for an `https` request scheme, which `tailscale serve`'s
`X-Forwarded-Proto` produces only on the forwarded-trust listener (task
#1892), since it terminates TLS and proxies plaintext to loopback; setting it
unconditionally would break localhost.

Public paths, reachable without a credential even under `UA_ENFORCE_AUTH=1`:
`/healthz` (content-free), `/readyz` (store readiness, no tenant or token
data), `/api/session` (gating the login route behind auth is a deadlock),
`/api/session/options`, `/api/session/workos/login`,
`/api/session/workos/callback`, `/favicon.ico` (requested automatically by
every browser, with no say from the page), `/ui` and its assets (they contain
no data; everything rendered comes from `/api`, which is gated), and when
`UA_MCP_OAUTH=1` the OAuth endpoints (`/authorize`, `/token`, `/register`,
`/revoke`, `/oauth/consent`, `/oauth/client-credentials`,
`/.well-known/oauth-*`). Those OAuth routes issue no hub tools; `/mcp` stays
bearer/OAuth-token gated.

`UA_MCP_OAUTH` is **off by default**. `/oauth/client-credentials` (RFC 6749
client credentials for pre-provisioned cloud bots, configured through
`UA_MCP_OAUTH_CLIENT_CREDENTIALS` and mapping each client onto an existing
hub agent label) stays inert until it is enabled; `system_doctor`'s
`mcp_oauth` check reports `client_credentials=off` in that state. See
[Grok Bot connector](grok-bot-connector.md) and
[Configuration](configuration.md). The hosted Railway runbook is
[deploy/railway.md](deploy/railway.md).

### Reading the anonymous-request count

The hub counts unauthenticated requests to non-public paths, rate-limited to
one `hub.auth.anonymous` event per minute. Two things to know before acting
on the number:

- **One hit per browser session is expected and harmless.** The page cannot
  see an HttpOnly cookie, so it must ask `/api/whoami` to find out whether it
  is logged in; before login that call is genuinely unauthenticated and is
  counted. It is deliberately *not* excluded: `/api/whoami` is a real API
  endpoint, and carving it out would leave a hole an actual unauthenticated
  client could sit in.
- **Sustained or repeating counts mean a real client is missing a token.**
  Check the `path` and `user_agent` on the events:
  `event_log_query(channels=["hub.auth.anonymous"])`.

### Where auth is configured

Secrets are **not** in `deploy/unified-agent.service`: `Environment=` values
are readable by any user via `systemctl --user show`. The convention is a
chmod-600 `~/.unified_agent/service.env` pulled in by a systemd drop-in
(`~/.config/systemd/user/unified-agent.service.d/search-key.conf`), which is
where `UA_AGENT_TOKENS` and `UA_ENFORCE_AUTH` live.

This means **the repo template and the running service differ by design**.
Reading the template alone will tell you auth is off when it is on; check
`systemctl --user show unified-agent -p EnvironmentFiles` and the drop-in
directory.

On this deployment auth is already enforced: an unauthenticated `POST /mcp`
returns 401, and Claude Code, the TUI and the executor each send a matching
bearer token. The web UI therefore works against the live hub as soon as it
restarts: sign in at `/ui` with any token from that file.

### Rolling it out on a hub that does not have it yet

**Stage A: identify, don't reject.** Add `UA_AGENT_TOKENS` but leave
`UA_ENFORCE_AUTH` unset, then watch the anonymous-request events
(`event_log_query(channels=["hub.auth.anonymous"])`). Every request becomes
attributed; nothing is rejected, so a client you forgot cannot be locked out
silently.

**Stage B: flip.** Once the 24h anonymous count has sat at zero, set
`UA_ENFORCE_AUTH=1`. Only `deploy/metrics.html` breaks (it sends no
`Authorization` header); `/ui` supersedes it.

> Add the executor's token before flipping. `system_doctor`'s
> `active_executor_service` check probes the executor's credential
> end-to-end and reports MISCONFIGURED with the 401 if it is missing. That
> check is the canary, and it is worth running immediately after any change
> to the token file.

> Never key auth off client IP in this deployment. `tailscale serve` proxies
> to `127.0.0.1`, so every tailnet request arrives looking like localhost: an
> IP-based bypass would silently make the whole tailnet anonymous.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `UA_HTTP_ALLOWED_ORIGINS` | `null` + hub origin + allowed hosts | Browser Origin allowlist; `host:*` matches any port |
| `UA_ENFORCE_AUTH` | `0` | Reject rather than warn on an unauthenticated request |
| `UA_UI_SESSION_TTL_HOURS` | `12` | Cookie session lifetime |
| `UA_UI_STREAM_POLL_S` | `1.0` | SSE cursor poll interval |
| `UA_UI_MAX_STREAMS` | `8` | Concurrent SSE streams before 429 |
| `UA_WEB_DIST_DIR` | unset | Serve the UI from this build directory instead of the packaged one |
| `UA_HEARTBEAT_MIN_INTERVAL_S` | `15` | Per-agent heartbeat write floor |
| `UA_AGENT_STATUS_THRESHOLDS` | `120,900,86400` | online / idle / stale cutoffs (seconds) |
| `UA_SESSION_ABANDON_MINUTES` | `30` | Reap a heartbeat-less running session |
| `UA_EVENT_BUS_EMBED` | `1` | Host the event bus in the hub process |
| `UA_MCP_OAUTH` | `0` | Mount the OAuth route table (`/oauth/*`, `/authorize`, …) |
| `UA_MCP_OAUTH_CLIENT_CREDENTIALS` | empty | `client_id\|client_secret\|agent_label` triples for `/oauth/client-credentials` cloud bots (each label must already exist) |

## Doctor checks

- **`web_ui`**: routes constructible and the UI build present. Catches the
  deploy failure where a hub never received the Vite build and `/ui` answers
  503 while development works.
- **`event_bus`**: LIVE means *a connection succeeded*, not that a file
  exists. The bus was importable for weeks while publishing nothing.
- **`agent_label_drift`**: labels active in the last 7 days that are absent
  from `UA_AGENT_LABELS`.
- **`package_checkout`**: DEGRADED when this process, or the main checkout's
  shared `.venv` editable pointer, is running a per-task worktree's code
  (task #1944). A worktree with its own venv is fine. See
  [tools-reference](tools-reference.md#system-doctor-toolsdoctorpy).
- **`agent_token_collisions`**: MISCONFIGURED names the labels that share one
  bearer (the hub refuses that token for all of them). Labels only, never a
  token value (task #1657).

## Operational notes

- Concurrency: the registry adds writers to a SQLite file already shared by
  hub, executor, cron and TUI. Mitigations are load-bearing: the 15s
  throttle, single-statement UPSERTs with no surrounding transaction, calm UI
  polling, the stream cap, and running every tool call and DB read through
  `anyio.to_thread`.
- The `control_flags` and `ui_sessions` tables are not required by the
  current UI action routes; authorization remains in the shared identity,
  CSRF, capability, and approval layers.
- If the UI looks wrong after a deploy, hard-refresh (`Cmd/Ctrl+Shift+R`) to
  drop any cached shell. There is no older UI to fall back to.
