# Control Center v2 build spec

Status: approved by the operator on 2026-11-10. Epic: task #2466.

This is the contract every Control Center v2 change builds against. It
replaces the 39-view Control Center with eight destinations. It supersedes
`control-center-information-architecture.md` for navigation and keeps
`command-center-design-protocol.md` §3 (tokens) and §5 (truth states).

## 1. Why

The audit of 2026-11-10 found 39 views, 23 of them read-only, in a UI that has
no router, shows zeros while data is loading, reports a parked approval as
success, and cannot create a task, write a memory note or add an agent. The
hub already does all of these; the UI does not reach them.

The platform goal is a meta-harness: organization, coordination, memory and
session synchronicity, handoffs, graph execution and planning, with local and
cloud agents in one organization. The UI must let one person do each of those
without reading documentation.

Four rules decide every design question below:

1. **Enable.** Every object the hub lets a person change can be changed here.
2. **Easy to understand.** Plain words. No internal names (lane, dispatch
   stack, truth state) in visible copy unless the page explains them.
3. **Familiar.** Lists, detail pages, forms, tabs and dialogs that behave like
   the ones in an issue tracker or a mail client. Back and forward work.
4. **Complete.** Nothing the legacy UI does is lost (§11 lists all 39 views).

## 2. Migration shape

Strangler. The new app lives in `packages/web/src/app/` beside the legacy
panes and is served from the same bundle.

- Legacy app: `/ui/` and `/ui/?view=<id>`. Unchanged until cutover.
- New app: any path with a segment after `/ui/`, for example `/ui/home`.
- `src/main.tsx` picks the app from `location.pathname`.
- Each app shows one link to the other ("Try the new Control Center", "Back to
  classic") until cutover.
- Cutover (§10) makes `/ui/` open the new app, redirects every `?view=` id and
  deletes the legacy panes.

No one edits a legacy file before cutover, except `src/main.tsx`,
`src/index.css` and `src/ui/**`, which the foundation task owns. To change a
legacy component, copy it into your area and edit the copy. Importing legacy
modules read-only is allowed and encouraged for data hooks and fetchers
(`src/hub.ts`, `src/api/*`, `src/eventStream.ts`, `src/attention`,
`src/approvals/useApprovals`, `src/sessions`, `src/polling.ts`).

## 3. Destinations and URLs

Router: `react-router` v7, `createBrowserRouter`, `basename: "/ui"`, every
area lazy-loaded. Links are real `<a>` elements (`<Link>`), so middle-click,
copy link and back/forward work.

The hub serves the SPA fallback only for extensionless paths
(`webui/spa.py`). **An id that can contain `.` or `/` goes in the query
string, never in the path** (vault paths, memory keys, file names).

| Destination | Path | What it answers |
|---|---|---|
| Home | `/home` | What needs me, what is running, what just finished |
| Inbox | `/inbox` | Everything waiting on a person, with the action inline |
| Work | `/work` | Tasks, goals and plans |
| Agents | `/agents` | Who is in the organization and what each can do |
| Sessions | `/sessions` | Live and past agent sessions |
| Knowledge | `/knowledge` | Memory notes and the vault |
| Runs | `/runs` | Workflows, graphs and cycles, with their history |
| Settings | `/settings` | Health, cloud, usage, credentials, integrations, audit |

Full route table (all under `/ui`):

```
/home
/inbox                      ?kind=approval|message|hold|proposal|anomaly  &item=<id>
/inbox/messages             conversations between agents and the operator
/work                       ?view=list|board|tree|graph  plus filters (§9.2)
/work/tasks/new
/work/tasks/:id
/work/goals        /work/goals/:id
/work/plans        /work/plans/new      /work/plans/:id
/work/canvas
/agents                     roster
/agents/add                 add a local or cloud agent
/agents/teams      /agents/skills      /agents/org      /agents/topology
/agents/hosts               devices and nodes
/agents/capabilities
/agents/profile/:label      agent page (never /agents/:label: a label may equal a fixed word)
/sessions                   ?status=open|ended|all
/sessions/new               start a session (dispatch)
/sessions/:id
/knowledge                  redirects to /knowledge/memory
/knowledge/memory           ?q=  &tag=  &note=<key>
/knowledge/vault            ?path=<vault path>  &q=
/runs                       overview: running now, recent
/runs/workflows    /runs/graphs    /runs/routines  /runs/cycles  /runs/cycles/:id
/runs/receipts     /runs/telemetry /runs/insights  /runs/executor
/settings                   redirects to /settings/health
/settings/health   /settings/platform  /settings/cloud  /settings/usage
/settings/credentials       /settings/audit  /settings/audit/provenance
/settings/integrations      /settings/integrations/slack|zulip|notifications
/settings/appearance
```

Filters, the selected tab and the selected row live in the URL
(`useSearchParams`), so a reload or a shared link restores the view.
`*` renders a not-found page with links to the eight destinations.

## 4. Shell

`src/app/shell/`. One sidebar, one top bar, one main region.

- **Sidebar**: the eight destinations, in the order above, icon plus label.
  Inbox carries a count from a complete read only; unknown shows "–", never 0.
  Collapses to an icon rail below 1280px or on `[`; a bottom tab bar on
  phones with Home, Inbox, Work, Sessions and "More".
- **Top bar**: breadcrumb built from route handles, a search field that opens
  the command palette (⌘K / Ctrl+K), one primary button "New" (menu: Task,
  Session, Plan, Memory note, Agent), and the account menu (who is signed in,
  theme, sign out, "Back to classic").
- **Main**: `<main id="main-content">`, max width 1440px. Each page renders
  its own `PageHeader`; the shell never draws a card around the page.
- **Section tabs**: an area with sub-pages renders `Tabs` as links under the
  page header (`role="navigation"`, not `role="tablist"`, because each tab is
  a URL).
- **Status bar**: hub connection (live stream state), last refresh, version.
- **Shortcuts**: `g` then `h i w a s k r t` for the destinations, `c` create
  task, `/` focus search, `?` shortcut help, `[` collapse. Never fire inside an
  input, textarea, select or contenteditable.
- **Sign-in**: 401 from `/api/whoami` renders the existing `SignInView`.
  After sign-in the user lands on the URL they asked for.
- **First run**: Home shows a checklist (connect an agent, start a session,
  create a task, save a note) until each is done; state from hub reads, not
  from local storage.

Focus moves to the page `h1` on navigation and the document title is
`<page> · Control Center`. The skip link stays first in the tab order.

## 5. Design system

Tokens stay as they are (`src/index.css`, `--ua-*`). The foundation task
defines the two names the code already uses but never declares: `--ua-line`
(alias of `--ua-border`) and `--ua-success` (alias of `--ua-ok`), in every
theme block, and adds them to `design-system/tokens.meta.json`.

`src/ui` gains the primitives below. New code uses them and nothing else for
these jobs: no raw `<button>`, `<table>`, `<input>`, `<select>`, `<dialog>`
in `src/app/**` outside `src/ui`. `window.prompt`, `window.confirm` and
`window.alert` are banned.

| Primitive | Contract |
|---|---|
| `Button`, `LinkButton`, `IconButton` | existing variants; `IconButton` requires `aria-label`; `loading` disables and announces |
| `Field` | label, hint, error, required mark; wires `htmlFor`, `aria-describedby`, `aria-invalid` |
| `Input`, `Textarea`, `Select`, `Checkbox`, `Switch`, `RadioGroup` | controlled and uncontrolled; 44px touch target on phones |
| `Combobox` | filterable single select (agent, project, parent task); ARIA 1.2 combobox pattern |
| `Dialog`, `ConfirmDialog`, `Drawer` | native `<dialog>` with focus trap, Escape, return focus; `ConfirmDialog` names the object and the consequence, `danger` variant for destructive actions |
| `Menu` | button plus menu, arrow keys, type-ahead |
| `Tabs` | link tabs (nav) and in-page tabs (tablist) as two components |
| `DataTable` | existing; add sortable headers, row link, empty and loading rows, sticky header, optional selection |
| `PageHeader` | `h1`, description, actions slot, optional tabs |
| `Toolbar`, `FilterBar` | search plus filter chips bound to URL params |
| `EmptyState` | what this is, why it is empty, one primary action |
| `ErrorState` | plain-language message, Retry, collapsed technical detail |
| `Skeleton` | loading placeholder; never a number |
| `Toast` (`useToast`) | `role="status"`; errors stay until dismissed |
| `Badge`, `StatePill`, `StatusIndicator`, `FreshnessStamp`, `AgentPersonaBadge`, `Card` | existing |
| `DescriptionList` | label and value pairs on detail pages |
| `CodeBlock`, `Markdown` | read-only rendering; `Markdown` sanitizes and never renders raw HTML |
| `GraphCanvas` | wrapper over `@xyflow/react` with pan, zoom, fit, keyboard focus on nodes and a table fallback for screen readers |

Every primitive has a unit test, appears in `UiShowcase`, and is described in
the design-system content so `npm run design-system` and
`tests/design-system.test.ts` stay green.

### Data states

Every query-backed region renders exactly one of: loading (`Skeleton`), error
(`ErrorState` with Retry), empty (`EmptyState`), ready. A count is shown only
from a successful, complete read; otherwise "–". `deriveTruthState` in
`src/ui/truthState.ts` is the helper. A stale read shows `FreshnessStamp`.

### Copy

Sentence case. Say what the thing is, then what the person can do. No
"lane", "truth state", "dispatch stack", "frontier", "red tier" without an
explanation on the page. Errors say what happened and what to do next; the
raw transport message goes in the collapsed detail.

## 6. Writes

All writes go through `src/app/lib/mutations.ts` (foundation task):

```ts
type WriteOutcome<T> =
  | { kind: "done"; data: T }
  | { kind: "accepted"; data: T }          // 202: work started, not finished
  | { kind: "pending_approval"; approvalId: string | null; tool?: string }
  | { kind: "error"; status: number | null; message: string; detail?: string };

function useHubWrite<TBody, TData>(opts: {
  path: string;
  method?: "POST" | "PUT" | "DELETE";   // default POST
  invalidates?: QueryKey[];             // refetched on done or accepted
  successMessage?: string | ((data: TData) => string);
  resumable?: boolean;                  // default true, see below
  idempotencyKey?: () => string;        // sent as Idempotency-Key, kept across retries
}): { run(body: TBody): Promise<WriteOutcome<TData>>; state: ... };
```

- Sends CSRF the same way `postCsrfJson` does (`x-ua-csrf`, one refresh on a
  CSRF 403). An empty successful body is valid. Sign-out
  (`DELETE /api/session`) and the canvas save (`PUT /api/graph-canvas`) use
  the helper too.
- **A parked approval is recognised the way the hub marks it**
  (`routes._is_parked_approval`, which the hub applies to 202 responses
  only): the status is 202 and the body has `code: "approval_required"`,
  or `approval_id`, or `global_approval_id`, or `pending: true`, or
  `global_approval_status: "pending"`, or an `autonomy_review` object.
  A 200 that happens to carry an `approval_id` (a released work product
  does) is a success. **HTTP 202 alone is not an approval, and neither is a
  bare `status: "pending"`**: a dispatch answers 202 with `status` `running` or
  `pending` for work that has been accepted. That is `accepted`, and the UI
  says the work has started.
- On `pending_approval` the UI shows "Waiting for approval" with a link to
  `/inbox?kind=approval&item=<id>`, keeps the form contents and changes
  nothing optimistically.
- **Approval does not run the write, and the UI never re-sends one by
  itself.** An approved row is a one-shot pass for the identical call
  (`tools/approvals.py`); the call has to be made again. Two review rounds
  showed that an automatic client-side replay cannot be made safe in a
  browser (two tabs, a duplicated tab, a lost response, a changed sign-in), so
  the repeat is always a person's click.
- A parked write is stored (`src/app/lib/parkedWrites.ts`, in the owner
  epoch's `sessionStorage`, see Owner epoch below):
  path, method, exact body, the approval id, the idempotency key if any, a
  label for the user, and the hub origin, organization and verified identity
  **that the request was actually sent under** (taken from the whoami read
  made for that send, not from a cache read afterwards).
- The form that parked and the Inbox item for that approval both show the
  stored write with its approval state, read through
  `GET /api/approvals/{id}` (never inferred from absence in the list):
  - waiting: "Waiting for approval";
  - approved: a **Run it now** button;
  - the signed-in person is the one who asked: the hub requires a different
    human to approve (separation of duties, `tools/approvals.py`
    `approval_resolve`), so the notice says "This needs approval from a
    different person" and the Inbox does not offer Approve on it. On a hub
    with one human operator such a request cannot be approved at all; the
    notice says so plainly (task #2500 asks the operator how to resolve
    this). **Approve and run** is offered only where one person may do both,
    which today is never;
  - denied, revoked or expired: says which, and offers to discard or to open
    the form.
- Run it now makes a fresh whoami read first and sends only if hub origin,
  organization and identity equal the stored ones; otherwise the entry is
  discarded with an explanation. It sends the identical body and idempotency
  key, once, with the button disabled while in flight. Success clears the
  entry. A repeat that parks again keeps the entry under the new approval. If
  the response is lost or the request is cut off after it started, the entry
  is marked **outcome unknown**: the UI says it could not confirm whether the
  change was made, links to the object to check, and does not offer another
  run until the user says it did not happen.
- `GET /api/whoami` does not return an organization today. Task #2469 adds
  the verified request's organization identifier to it, with an explicit
  value for unpartitioned mode. Until the hub reports one, a stored write
  cannot be run from storage and the UI offers "Open the form to run it".
- Stored writes and drafts are dropped the moment sign-out begins, before any
  network request of the sign-out itself.
- A write is `resumable` only when repeating it is proven to rebuild the
  identical approval payload, with a test. Otherwise it sets
  `resumable: false`, nothing is stored, and the form says "After it is
  approved, submit this form again". Never resumable: a write whose body
  carries a secret, a session launch (the hub creates a delegation before a
  later gate), and a work-product submission (resubmitting re-hashes the file
  and can create a second submission). A parked work-product release continues
  through `POST /api/work-products/{id}/release`.
- 401 routes to sign-in. 403 says the signed-in identity may not do this and
  names the capability when the hub returns it. 409 says the object changed
  and offers Reload.
- A destructive write always goes through `ConfirmDialog`.
- Forms keep a draft in the owner epoch's `sessionStorage` until the write is done. The key is
  the signed-in identity, the form's operation and the object's identity
  including the query parameters that select it (for example
  `agent-claude|memory.edit|note=<key>`). A draft is restored only on an
  exact key match and is cleared on success and on sign-out. Tokens and
  credentials are never written to a draft.
- No write fires from a bare key press on a focused row. Keyboard status moves
  on the board open a confirm.

### Owner epoch

Every write rule above fails in the same way if an operation started by one
signed-in person has an effect after someone else is signed in. Review
round 7 replaced per-call checks with one object: the **owner epoch**
(`src/app/lib/epoch.tsx`).

- **One epoch per verified owner in a tab.** The owner is hub origin,
  organization and identity from a successful `GET /api/whoami` that names
  an identity. The epoch owns a `QueryClient` for all app data, owner-scoped
  `sessionStorage` (`epoch.storage`) and `localStorage` plus Web Locks
  (`epoch.sharedStorage`), each write confirmed by reading it back, an
  `AbortSignal`, `wrap()` for slice callbacks and `toast()`. whoami itself
  lives in the root `QueryClient`, which holds nothing else.
- **Revocation.** An epoch is revoked by a whoami answer naming a different
  owner, by sign-out, by a new auth generation (a 401 seen by any write),
  and by any message on the `ua-cc2-auth` BroadcastChannel (another tab
  signed in or out). Revocation is synchronous: the signal aborts, the
  client's queries are cancelled and cleared, storage reads return the
  fallback and writes return false, toasts and wrapped callbacks do
  nothing, and the client takes no new work (a query built after
  revocation never fetches). After a 401 or a channel message only a whoami
  answer newer than the revocation names the next owner.
- **The routed tree is keyed by the epoch.** `OwnerEpochProvider` renders
  the epoch's `QueryClientProvider` and `ToastProvider` keyed by its id, so
  a new owner gets a new tree with none of the old one's state or toasts.
- **Operations capture the epoch when they start** and act only through it
  afterwards: `useHubWrite`, `runParkedWrite`, `addParkedWrite`, drafts,
  `ParkedWriteNotice` handlers. Each re-checks the owner after every await
  and once more before returning to its caller, so a caller's continuation
  (navigate, clear a draft) never runs under the next owner.
- **Sign-out is an operation of the epoch it started in.**
  `beginSignOut(from)` refuses (returns null) when `from` is not the current
  epoch, so a click from an old tree never signs out whoever is signed in
  now. Drafts and stored writes are dropped under `from` first. It then
  revokes `from`, tells other tabs, and returns a **lease** on exactly one
  transition: `from` replaced by the owner-null epoch the sign-out made.
  Anything after that (a whoami naming another owner, a 401, a sign-in in
  this tab or another) ends the lease: the request's signal aborts, nothing
  is sent if it has not left, and `finish` does nothing, so a late answer
  never touches the next owner's epoch or whoami. The request is also bound
  to the owner it signs out (`SignOutHandle.owner`): the whoami read that
  issues its CSRF token, and the retry's, must name that owner, because the
  hub's cookie can belong to someone else while the page's cached whoami
  still says A; on a mismatch nothing is sent. The client cannot close the
  last gap alone (the cookie can change after the retry's whoami read and
  before its DELETE), so the hub closes it: `DELETE /api/session` revokes a
  live session only with that session's own CSRF token, and otherwise
  answers 403 `csrf_session_mismatch` and changes nothing. A failed session
  lookup (a lock timeout included) is never read as "no session": it
  answers 503 `session_lookup_failed` and changes nothing; only a confirmed
  absence clears the cookie. The client
  treats that answer as "not signed out": it reads whoami again and never
  resets it. A failed sign-out request reads whoami again instead of
  clearing it.
- **Late completions stay with their owner.** A 401 ends the sign-in only
  when the write that saw it still belongs to the current owner: the
  owner check comes before the new auth generation, so A's late 401 never
  revokes B. A sign-in confirmation belongs to the epoch its form was
  shown in (`signedIn(from)`, the callback wrapped by that epoch): one
  that arrives after someone else became the verified owner does nothing.
- **Memory follows the epoch too.** Caches that hold request bodies (the
  parked-write listing) are keyed by epoch and dropped on revocation and
  on sign-out.
- **Enforced by tests.** `tests/app/architecture.test.ts` forbids Web
  Storage, the storage module's raw helpers, direct toasts
  (`useToast`/`ToastProvider`), any `QueryClient` made or provided, and a
  completion callback (`onSuccess`, `onComplete`, ...) handed on without
  the epoch's `wrap`, in `src/app` and every module it imports from `src/`
  (the sign-in view, the ui primitives), except `epoch.tsx`, `returnTo.ts`
  for the pre-sign-in return path, and the listed primitives (the toast
  component, the per-device theme).
  `tests/app/lib/owner-switch.test.tsx` switches the owner at each stage
  boundary (before send, after send, after the answer, during the toast) of
  every write path and asserts that no storage write, cache write, toast,
  callback, navigation or request happens after the switch. It also moves
  the hub's cookie apart from the cached whoami (before the send and before
  a CSRF retry), and delivers a 401 and a sign-in confirmation after B is
  verified.

Accepted departures from the round-7 design:

1. **A transient whoami failure does not revoke.** Only a 401, an identity
   change, sign-out or a channel message does. A 5xx or network failure
   leaves the epoch live, but the per-send checks still refuse: a write
   needs a successful whoami read naming its owner right before it is
   sent, so nothing is sent while who is signed in is unknown.
2. **Sign-out is bound to its starting epoch through the lease above**, not
   run inside the starting epoch (which it revokes first). The lease gives
   the same guarantee: its completion has an effect only while nothing has
   happened since it began.
3. **The routed tree mounts only after whoami first answers.** Until then
   there is no owner, and mounting the page would only remount it and
   refetch everything when the owner arrives. The shell shows a polite
   "Checking who is signed in" status instead.

## 7. Layout, tests and checks

```
packages/web/src/app/
  App.tsx  router.tsx  routes.ts        foundation
  shell/   lib/                          foundation
  home/ inbox/ work/ agents/ sessions/
  knowledge/ runs/ settings/ integrations/   one area each
packages/web/tests/app/
  <area>/*.test.tsx                      unit and component (Vitest, jsdom)
  <area>.e2e.spec.ts                     Playwright flows, hub mocked with page.route
  <area>.a11y.spec.ts                    axe, every route of the area in 3 themes
  support/                               foundation: render helper, hub mocks, fixtures
```

Each area owns its directory and exports from `routes.tsx`:

```ts
export const routes: RouteObject[];             // mounted under the area path
export const commands?: PaletteCommand[];       // command palette entries
```

The foundation task creates every area directory with a `routes.tsx` stub that
renders a "coming soon" `EmptyState`. A slice changes only its own area
directory and its own tests. It adds new fetchers in `src/app/<area>/api.ts`
and does not edit `src/api/*`, `src/hub.ts`, `package.json` or the lockfile.
A slice that needs a new shared primitive or dependency says so in its PR
body under "Needs from foundation" and builds a local version in its area.

Paths that match `cycle_eligibility.RED_TIER_PATTERNS` escalate to the
operator, and the patterns are unanchored regular expressions, so check a new
path against the list itself. Two traps: `hooks?/` matches any directory whose
name ends in `hook` or `hooks` (`hooks/`, `webhooks/`), and `\.env` matches
any path containing `.env`. Put React hooks in `lib/` or beside the component
and name a webhook page `notifications` or `web-hooks.tsx` as a file, never a
directory.

Checks before a push, from `packages/web`: `npm run typecheck`, `npm test`,
`npm run build`, then `npx playwright test tests/app/<area>` for the area's
e2e and a11y specs. Every interactive flow in §9 has an e2e test that drives
it with the keyboard at least once. Every page passes axe (WCAG 2.2 AA) in
dark, light and high-contrast, at 1440px and 390px wide.

## 8. Hub routes to add

Conventions (follow the existing ones in `webui/routes.py`):

- Reads are `GET /api/...`. Writes are `POST /api/action/...` with a JSON
  body, guarded by `_guard_mutation` (CSRF).
- **A handler calls the registered tool through `webui.api.call_tool` (or the
  registered platform writer the neighbouring handlers use), never the bare
  function in `tools/`.** The policy gate and the approval gate live in the
  registration wrapper in `server.py`; importing the implementation skips
  them. A consolidated manager action is declared in `Scope.actions`.
- No second permission model. Identity comes from the verified request, never
  from the body.
- A parked approval answers 202 through `_approval_pending` / `_json`, which
  adds `code: "approval_required"`. `ToolError` answers 400 with
  `{"error": ...}`; not found is 404; unknown body fields are 400.
- Every `(method, path)` has an entry in `webui/route_scopes.py`. A write is
  `CAPABILITY` with the tool or action it calls and `operator=True`, so an
  agent is held to its profile ceiling and the human operator can act from
  the browser. A route for the operator alone is `operator=True` with no
  tools or actions, and its handler also checks for the verified operator.
- A fixed path is registered before any dynamic path that could match it.
- New handlers live in new modules registered from `_declared_routes()`:
  `webui/cc_knowledge.py`, `webui/cc_work.py`, `webui/cc_agents.py`.
- `webui/routes.py`, `webui/route_scopes.py`, `tests/test_route_scopes.py` and
  `docs/control-center-api.md` are shared files. **The hub tasks therefore run
  one after another, each branch stacked on the previous one** (#2469, then
  #2480, then #2470). A front-end slice codes against
  `docs/control-center-api.md` on the newest hub branch.
- Tests go in `tests/test_webui_cc_<domain>.py`.

Routes that already exist and must be reused, not twinned:

| Route | Use |
|---|---|
| `POST /api/tasks`, `POST /api/goals` | create a task, create a goal |
| `POST /api/action/task` | every `task_update` field in its whitelist |
| `/api/tasks/{id}/claim-release` | release a claim |
| `GET, POST /api/tasks/{task_id}/comments` | task comments |
| `POST /api/action/plan` | plan review `intent` only; kept as it is |
| `POST /api/action/work-proposal-decide` | decide a work proposal (`coordination_manage`) |
| `POST /api/action/workflow` | run a reviewed workflow |
| `POST /api/routines/{routine_id}/fire` | bearer-only API and webhook endpoint (refuses cookies, needs an `Idempotency-Key`, refuses cron routines); not used by the browser |
| `GET, PUT /api/graph-canvas` | planning canvas |
| `/api/evidence`, `/api/work-products`, `/api/action/harness-config`, `/api/action/session-credential-revoke`, evidence export `?format=pdf|csv` | never called by the legacy UI |
| reads: `/api/runs`, `/api/runs/{id}`, `/api/sessions/{id}`, `/api/agents/{label}`, `/api/personas`, `/api/goals/{id}`, `/api/org-proposals`, `/api/digest`, `/api/vault/ingest-status`, `/api/tasks` | never called by the legacy UI |

Task #2469, adapters over tools that exist today:

| Module | Route | Registered tool |
|---|---|---|
| knowledge | `GET /api/memory/note?key=` | `memory_get` |
| knowledge | `POST /api/action/memory/save` | `memory_save` (author is the verified identity; replacing another author's note needs `overwrite: true`) |
| knowledge | `POST /api/action/memory/delete` | `memory_delete` |
| knowledge | `POST /api/action/memory/restore` | `memory_restore` |
| knowledge | `POST /api/action/memory/promote` | `memory_promote` |
| knowledge | `POST /api/action/memory/feedback` | `memory_feedback` |
| knowledge | `GET /api/vault/list?dir=` | `vault_list` |
| knowledge | `GET /api/vault/links?path=` | `vault_links` |
| knowledge | `POST /api/action/vault/write` | `vault_write` |
| knowledge | `POST /api/action/vault/journal` | `vault_journal` |
| work | `POST /api/action/plan/{action}` for create, update, checkpoint, resume, retry, claim, handoff, close, delete | one route and one scope entry per action, each carrying exactly its `(plan_manage, <function name>)` capability. A scope requires every action it lists, so alternatives are never listed together. The existing `POST /api/action/plan` review route is untouched |
| work | `POST /api/action/plan/joint/{action}` for bind, propose, review, decide, recover | same rule, one scope each. Office membership, peer review and authored-comment rules stay in force: `operator=True` does not override them |
| work | `GET /api/plans/{plan_id}/joint` | `plan_manage` `joint_get`: current revision, pending proposal, team, and what the caller may do |
| work | `POST /api/action/task/claim` | `task_claim` for the signed-in identity |
| work | `POST /api/action/task/delete` | `task_delete` |
| work | `POST /api/action/task` | add `archived` to the whitelist (archive and unarchive) |
| work | `POST /api/action/langgraph/run` | `langgraph_workflow_run` / `langgraph_run` |
| work | `POST /api/action/session/end` | `session_end`: a caller ends its own session only |

Task #2480, new operations the hub does not have yet (tool changes with
tests, then their routes):

| Module | Operation | Notes |
|---|---|---|
| work | plan structure: add a step, edit a step's text, reorder steps, edit the plan title and description | new `plan_manage` actions; exposed to the profiles that already have `plan_update`, no wider |
| work | goal edit, link and unlink a task, close and reopen | new goal actions and `POST /api/action/goal` |
| work | `POST /api/action/session/operator-end` | a new `session.operator_end` platform writer called through the governed write adapter, attributed to the operator and recorded in the evidence ledger. The verified identity is never cleared or replaced to make `session_end` pass |
| runs | `GET /api/routines`, `POST /api/action/routine` (add, pause, resume) | over `routines.routine_list`, `routine_add`, `routine_pause` |
| runs | `POST /api/action/routine/run` | operator "Run now", CSRF-protected, works for scheduled routines, with a stable idempotency key and documented behaviour when a run is already active |

Before any front-end slice that depends on #2480 starts, #2480 publishes the
request, response, permission and conflict contracts in
`docs/control-center-api.md`, including where a plan's title and description
are stored (a plan has a `goal` field today) and how step ids and
dependencies are remapped on reorder. #2480 must not need a new top-level
tool; if it does, `server.py` is red-tier scope and needs the operator's
approval first.

Task #2470, stacked on PR #1497 (which must merge first):

| Module | Route | Service |
|---|---|---|
| agents | `GET /api/agent-roster` | `agent_enrollment.list_agents` |
| agents | `POST /api/action/agent/add` | `agent_enrollment.add_agent`: local or cloud; operator only; the one-time token is returned once and never stored by the UI |
| agents | `POST /api/action/agent/remove` | `agent_enrollment.remove_agent`; operator only |
| agents | `POST /api/action/connector/register` | `horizon2_server_register` / `horizon2_package_register` |
| agents | `POST /api/action/connector/authorize` | `horizon2_connector_authorize` |
| agents | `POST /api/action/connector/revoke` | `horizon2_server_revoke` |
| agents | `POST /api/action/org-proposal` | `org_proposal` review, apply, reject (the actions that exist), each through its approval step |

Enrolment functions are services, not registered tools, so their routes are
the operator's alone and are invoked the governed way PR #1497's CLI does.

Status (task #2470): built in `webui/cc_agents.py` and documented in
`docs/control-center-api.md` (section `agents`). Two deviations from the table:
`register` is two routes, `POST /api/action/connector/register`
(`horizon2_server_register`) and `POST /api/action/connector/register-package`
(`horizon2_package_register`), because a scope requires every tool it lists;
and `POST /api/action/org-proposal` takes `action: review|apply|reject`, with
apply and reject reserved for the verified human operator. Work-proposal
decisions keep the existing `POST /api/action/work-proposal-decide`.

Status (task #2516): the Runs, Work and Settings hub gaps are built and documented
in `docs/control-center-api.md`: one `Idempotency-Key` scheme ("Keyed writes") on
plan create, routine create, workflow run, graph run, cycle start and
session-credential revoke; `GET /api/tasks/{task_id}/subtree`;
`GET /api/langgraph/runs/{run_id}`; `GET /api/session-credentials`; `/api/evidence`
keyset paging and `GET /api/evidence/export`. LangGraph resume (item q) is its own
task.

## 9. Areas

Each subsection lists what the area must do. "Must" items are acceptance
criteria. Read the legacy panes named in §11 for the behaviour to retain.

### 9.1 Home and Inbox (`home/`, `inbox/`)

Home:
- Three regions: **Needs you** (top five Inbox items with the action inline
  and "See all"), **Running now** (open sessions and active runs with agent,
  task and elapsed time), **Just finished** (work closed or sent for review
  in the last 24 hours).
- A project filter that scopes all three and persists in the URL.
- Each region has its own loading, error and empty state. No count renders
  before its read completes.
- First-run checklist (§4) while the organization is empty.
- Fleet strip: one line per agent with state; links to the agent page.

Inbox:
- One list of everything waiting on a person, from the typed inbox
  (`needs_operator`: approval, operator task, hold, held pull request,
  operator reply, wake grant), pending approvals, work and org proposals,
  memory proposals, and anomalies. Tabs filter by kind; counts per tab.
- List on the left, detail on the right (stacked on phones); `j`/`k` move,
  Enter opens. Selection is in the URL.
- Every item kind has its actions in the detail pane: approve or deny an
  approval with a reason; reply to an operator question; grant or refuse a
  wake; release or keep a hold; decide a proposal item by item; open the
  related task, session or pull request.
- Approving shows what will run (tool, arguments summary, requester) before
  the button is enabled, and the result afterwards.
- `/inbox/messages`: conversations with agents, with send, reply and hand off
  to a task. Retains legacy Messages.

### 9.2 Work (`work/`)

- One task set, four views over the same filters: **List** (sortable table),
  **Board** (columns by status), **Tree** (parent and subtasks, collapsible),
  **Graph** (dependencies and work graph on `GraphCanvas`).
- Filters: status, project, domain, priority, owner, lane (labelled "Queue"),
  text search, "mine", "blocked", "waiting for review". All in the URL.
- **Create task** (`/work/tasks/new` and `c`): title, description, priority,
  project, parent, queue, due date. Posts to `POST /api/tasks`.
- **Task page**: title and description editable in place, status control,
  priority, owner, parent, project, due date, subtasks, dependencies,
  activity (notes, runs, evidence, pull request), comments. Actions: claim,
  release claim, send to an agent (opens `/sessions/new?task=<id>`), archive,
  delete (confirm). Every `POST /api/action/task` field is reachable.
- Board: drag between columns with a pointer; with the keyboard, a "Move to"
  menu on the card. A move that needs evidence or review explains why it was
  refused.
- Graph: pan, zoom, fit, select a node to open the task drawer, correct edge
  labels (blocks / blocked by / parent of), legend, and a table fallback.
- **Goals**: list, goal page with linked tasks and progress, create and edit.
- **Plans**: list, plan page with ordered steps, step status, checkpoints;
  create a plan, add, edit and reorder steps, hand a step to an agent.
- **Canvas**: the Excalidraw planning canvas, retained.
- **Shared plans**: propose, review and decide a joint plan, and recover a
  conflicted one, on the plan page. These need office membership; anyone
  else, the operator included, sees the action disabled with the reason.
- **Evidence and work products**: on the task page, attach evidence
  (`/api/evidence`) and submit a work product for release
  (`/api/work-products`), with the approval step followed inline.

### 9.3 Agents (`agents/`)

- **Roster**: every agent in the organization, local and cloud, with persona,
  host, state (working, idle, paused, offline, retired), current task and last
  seen. Filter by host kind and state.
- **Agent page**: what it is doing now, recent sessions and tasks, what it may
  do (profile, capabilities in plain words), where it runs (host, harness,
  workspace), skills, credentials status (present or missing, never the
  value). The page lives at `/agents/profile/:label`. Actions: start a session, send a message, pause, resume, retire
  (confirm), remove (confirm).
- **Add agent** (`/agents/add`): a three-step form. 1. Where it runs: this
  machine, another machine, a cloud host. 2. Which harness and label, with
  the role it gets. 3. Connect: the exact command or config to paste on that
  host, the one-time token shown once with a copy button, then a live check
  that turns green when the agent's first session arrives. Operator only;
  others see why the button is disabled.
- **Teams** and **Skills**: retained from Skills & Teams and Operating,
  including team boot with its approval step explained inline.
- **Org** and **Topology**: the org chart and the agent topology on
  `GraphCanvas`.
- **Hosts**: devices and nodes, join a device, acceptance status, node
  controls, and publishing a harness configuration bundle
  (`/api/action/harness-config`) with its approval step. Retains legacy
  Devices.
- **Capabilities**: the capability catalog. Retains legacy Capabilities.

### 9.4 Sessions (`sessions/`)

- **List**: open sessions first, with agent, task, host, started, last
  activity, state. Filters for status, agent and host; search.
- **Session page**: a chat-style transcript (operator messages, agent
  messages, tool activity collapsed by default), live through the shared
  event stream, with a composer for a follow-up. Side panel: task, agent,
  host, cost and tokens when reported, files and pull request.
- Actions: stop (confirm), resume, hand off to another agent (pick agent,
  add a note), end session, grant a wake, open the crew view when the session
  has one.
- **Start a session** (`/sessions/new`): pick the agent (or "let the hub
  choose"), the task or a free prompt, the host. The guardrails line shows the
  real policy state from the hub, never a constant. If the launch needs an
  approval, the page says so and follows the approval instead of asking the
  user to submit again.
- Retains everything the legacy Sessions and Dispatch panes do, including the
  dispatch preview.

### 9.5 Knowledge (`knowledge/`)

- **Memory**: search (hybrid), filter by tag, author and project. Opening a
  note shows the full body rendered as Markdown, tags, author, dates and
  provenance.
- Create a note; edit (replace or append); delete (confirm) and restore;
  promote; mark helpful or wrong (feedback). Editing another author's note
  warns and requires the explicit overwrite choice.
- Memory proposals link to the Inbox.
- **Vault**: a folder tree, a note reader (Markdown, wikilinks navigate,
  backlinks listed), search, ingest status per note. Write a new note or
  append to one in the writable folders only; a locked note says it is locked.
  Add a journal entry.

### 9.6 Runs (`runs/`)

- **Overview**: what is running now and the last 50 runs with status,
  duration, agent and cost.
- **Workflows**: the reviewed workflow list, run one with its inputs, see step
  results.
- **Graphs** (LangGraph): start a run, watch progress on `GraphCanvas`, see
  checkpoints, resume from a checkpoint.
- **Routines**: scheduled work: list with next run and history, create with
  a schedule, pause, resume, run now (`POST /api/action/routine/run`).
- **Cycles**: list and detail; start, pause, stop with forms and confirm
  dialogs (no `window.prompt`), never posting without a task.
- **Receipts**, **Telemetry**, **Insights** (the legacy Flow analytics) and
  **Executor**: retained as history and status pages.

### 9.7 Settings: system (`settings/`)

- **Health**: the doctor report in plain words, grouped by "Working",
  "Needs attention", "Off", each row with what to do.
- **Platform**, **Cloud**, **Usage** (quota and spend), **Credentials**
  (pool status, revoke a session credential with confirm; values are never
  shown).
- **Audit**: the evidence ledger with filters, verify chain, export PDF or
  CSV; **Provenance** as a sub-page.
- **Appearance**: theme (light, dark, high contrast, system) and density.
- `settings/routes.tsx` mounts the integrations area under
  `/settings/integrations` (the foundation stub does this).

### 9.8 Settings: integrations (`integrations/`)

- **Integrations**: connectors with connect (OAuth begin), status, revoke;
  register a connector server or package (new route).
- **Slack**, **Zulip**, **Notifications**: retained from the legacy panes.

## 10. Cutover

One PR after every slice has merged:

- `/ui/` renders the new app; `/ui/?view=<id>` redirects per §11 for every
  legacy id and alias, including `#<id>` hashes.
- Delete every legacy pane, shell file and test that the new app no longer
  imports; move what it still imports under `src/app` or `src/lib`.
- `tests/nav.test.ts` and `tests/certification.a11y.spec.ts` assert the eight
  destinations and every route in §3.
- Rewrite `docs/web-ui.md` for the new UI; remove stale design docs from the
  docs index or mark them superseded.
- A changelog fragment in `changelog.d/`.

## 11. Legacy view map

Every legacy view id, where it goes, and which area owns it.

| Legacy `?view=` | New path | Area |
|---|---|---|
| home | `/home` | home |
| attention | `/inbox` | inbox |
| approvals | `/inbox?kind=approval` | inbox |
| messages | `/inbox/messages` | inbox |
| proposals | `/inbox?kind=proposal` | inbox |
| tasks | `/work` | work |
| board | `/work?view=board` | work |
| task-graph | `/work?view=graph` | work |
| work-graph | `/work?view=graph&scope=work` | work |
| goals | `/work/goals` | work |
| plans | `/work/plans` | work |
| canvas | `/work/canvas` | work |
| sessions | `/sessions` | sessions |
| dispatch | `/sessions/new` | sessions |
| org | `/agents/org` | agents |
| agent-topology | `/agents/topology` | agents |
| skills-teams | `/agents/teams` | agents |
| operating | `/agents/teams?tab=operating` | agents |
| capabilities | `/agents/capabilities` | agents |
| devices | `/agents/hosts` | agents |
| memory | `/knowledge/memory` | knowledge |
| vault | `/knowledge/vault` | knowledge |
| cycles | `/runs/cycles` | runs |
| langgraph | `/runs/graphs` | runs |
| executor | `/runs/executor` | runs |
| telemetry | `/runs/telemetry` | runs |
| dispatch_receipts | `/runs/receipts` | runs |
| flow | `/runs/insights` | runs |
| doctor | `/settings/health` | settings |
| platform | `/settings/platform` | settings |
| cloud | `/settings/cloud` | settings |
| quota | `/settings/usage` | settings |
| credential_pool | `/settings/credentials` | settings |
| evidence | `/settings/audit` | settings |
| provenance | `/settings/audit/provenance` | settings |
| connectors | `/settings/integrations` | integrations |
| slack_gateway | `/settings/integrations/slack` | integrations |
| zulip | `/settings/integrations/zulip` | integrations |
| notify_delivery | `/settings/integrations/notifications` | integrations |

Aliases: `actions`, `decisions` → approvals; `portfolio`, `overview` → home;
`queue`, `delivery` → tasks; `kanban` → board; `topology` → agent-topology;
`organization` → sessions; `knowledge` → memory; `analytics` → flow;
`audit` → evidence.

## 12. Defects the rebuild must not carry over

Each has a test in the area that owns it.

1. A 202 pending approval shown as success (task detail, board).
2. Board arrow keys changing status with no confirmation.
3. "Guardrails on" shown as a constant in the launch form.
4. Zero counts on Home while reads are loading or failed.
5. Navigation from a banner dropping its parameters.
6. Wrong relation labels on graph edges.
7. `window.prompt` in Cycles, and a cycle action posted with an empty task id.
8. Drafts lost on navigation.
9. "Approve in Approvals, then submit the same again" dead ends.
10. Raw transport errors shown to the user.
11. A sign-in page that asks for an "Agent Bearer Token" with no explanation.
12. Tabs ordered by accident of declaration.

## 13. Definition of done for a slice

- Every "must" in its §9 subsection works against the real hub contract and
  is covered by a component or e2e test.
- Every legacy view mapped to the area in §11 is reachable at the new path
  with nothing removed, or the PR body lists what moved where.
- `npm run typecheck`, `npm test`, `npm run build` and the area's Playwright
  specs pass; results pasted in the PR body.
- No edits outside the area's file scope.
- PR body: task id, what changed, verification commands and output, "Needs
  from foundation", "Found, not fixed", and the line
  `Red-tier files: none` (or the list).
