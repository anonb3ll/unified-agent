# Wayfinder pane-parity audit + `/ui/legacy` cutover criteria

> Superseded for navigation and panes by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479).

**Task:** #885 (initiative — Modernize Unified Agent Web UI).
**Author:** agent-claude, 2026-09-10.
**Verified against:** `main @ 31c5349`; `src/unified_agent/webui/static/index.html`
(legacy panes), `packages/web/src/shell/nav.ts` + `packages/web/src/App.tsx`
(modern NAV), `docs/web-ui.md` pane/endpoint table.

This is the planning artifact for the final stretch of #885: an authoritative
enumeration of every Control Center pane, its port status, confirmation that
child #918 is the complete remainder, and the criteria that must all be true
before `/ui/legacy` is retired.

---

## 1. Pane inventory (24 legacy panes)

The legacy shell has **10 top-level modes** (`data-mode` on the tab buttons:
`attention board graph canvas operations evidence approvals delegations memory
devices`) containing **24 `id="pane-*"` sections**. The modern SPA
(`nav.ts`) exposes **20 nav modes**, each a `role="radio"` rendering one pane.

| # | Legacy pane (`id`) | Legacy mode | Heading | Modern pane (`NavId`) | Status |
|---|---|---|---|---|---|
| 1 | `pane-attention` | attention | Attention | `attention` | ✅ ported (#893) |
| 2 | `pane-portfolio-map` | attention | Portfolio Map | `home` | ✅ ported (#918) |
| 3 | `pane-board` | board | Kanban Board | `board` | ✅ ported (#894) |
| 4 | `pane-telemetry` | graph | Run Waterfall | `telemetry` | ✅ ported (#918) |
| 5 | `pane-agent-canvas` | graph | Agent Topology &amp; Workflow Execution Canvas | `agent-topology` | ✅ ported (#918) |
| 6 | `pane-task-graph` | graph | Task &amp; Plan Graph (lineage) | `task-graph` | ✅ ported (#918) |
| 7 | `pane-execution-graph` | graph | Execution Graph | `flow` | ✅ ported (#895) |
| 8 | `pane-sessions` | graph | Sessions | `sessions` | ✅ ported (#900) |
| 9 | `pane-agents` | graph | Agent Activity | `sessions` (folded) | ✅ ported (#900) |
| 10 | `pane-planning-canvas` | canvas | Planning Canvas | `canvas` | ✅ ported (#896) |
| 11 | `pane-agent-canvas` dup ref | — | — | — | (see #5) |
| 12 | `pane-action-deck` | operations | Action Deck | `actions` | ✅ ported (#901) |
| 13 | `pane-activity` | operations | Activity | `sessions` (folded) | ✅ ported (#900) |
| 14 | `pane-cycles` | operations | Cycle cockpit | `cycles` | ✅ ported (#903) |
| 15 | `pane-dispatch` | operations | Dispatch Studio | `dispatch` | ✅ ported (#901) |
| 16 | `pane-executor-delegation` | operations | Executor &amp; Delegations | `executor` | ✅ ported (#903) |
| 17 | `pane-langgraph` | operations | LangGraph Checkpoints | `langgraph` | ✅ ported (#918) |
| 18 | `pane-queue` | operations | Queue &amp; Health | `tasks` | ✅ ported (#886) |
| 19 | `pane-review-approval` | operations | Review &amp; Approvals | `approvals` | ✅ ported (#897) |
| 20 | `pane-zulip-coordination` | operations | Zulip Coordination | `zulip` | ✅ ported (#918) |
| 21 | `pane-evidence` | evidence | Evidence | `evidence` | ✅ ported (#899) |
| 22 | `pane-approvals-queue` | approvals | Approvals queue | `approvals` | ✅ ported (#897) |
| 23 | `pane-delegations-ledger` | delegations | Delegations ledger | `executor` | ✅ ported (#903) |
| 24 | `pane-memory-browser` | memory | Memory browser | `memory` | ✅ ported (#903) |
| 25 | `pane-devices` | devices | Devices | `devices` | ✅ ported (#903) |

(Rows 5 and 11 are the same `pane-agent-canvas` section; the table lists 24
distinct panes.)

### Roll-up

- **Ported: 24** — all distinct legacy panes have modern equivalents.
- **Not ported: 0**.
- **Folded (no standalone nav mode, verify parity when closing #885):**
  `pane-agents` and `pane-activity` both render inside the modern `sessions`
  pane — `SessionsPane.tsx` pulls live `GET /api/agents` for the activity table
  and `useActivityStream` for the feed. Treat as ported; spot-check the agent
  activity table has column parity before final sign-off.

---

## 2. Is #918 the complete remainder?

**Yes.** #918's six panes (`portfolio-map`, `telemetry`, `agent-topology`,
`task-graph` depth, `zulip`, `langgraph`) are exactly the set of legacy
`id="pane-*"` sections with no modern equivalent. Enumerated both directions:

- Every legacy pane not in #918's list maps to a shipped `NavId` (table above).
- Every #918 pane has a legacy `id="pane-*"` section and a modern module under
  `packages/web/src/` (landed in PR #484, commit `00ebc5a`).

No gap task needs to be filed. #918 is complete and stays the last child of #885.

### Endpoint notes for #918 (verified against `routes.py @ 31c5349`)

| #918 pane | Legacy renderer | Backing API today | New route needed? |
|---|---|---|---|
| portfolio-map | `renderPortfolioMap` / `#portfolio-grid` | `GET /api/overview` (portfolio snapshot) | **No** — reuse `/api/overview` |
| telemetry | `renderTelemetry` / `#telemetry-body` | `GET /api/telemetry` (`routes.py:1070`) | **No** — route exists |
| agent-topology | `layoutTopology` / `pane-agent-canvas` SVG | `GET /api/agents` (`:1045`) + `GET /api/execution-graph` (`:1059`) | **No** — compose the two client-side |
| task-graph (depth) | `#pane-task-graph` SVG, lane/agent/status filters | `GET /api/execution-graph?task=` (`:1059`) | **No** — Flow already calls it; needs the filtered lineage view + filters |
| langgraph | `renderLangGraphCheckpoints` / `#langgraph-body` | `GET /api/langgraph/checkpoints` (`:1066`) | **No** — route exists |
| zulip | `pane-zulip-coordination` | `GET /api/zulip` | **No** — read-only modern pane; no send from the UI without an approval gate |

There is no remaining backend gap for the six panes. Everything else is a pure
`packages/web` client task, and the Zulip pane uses the existing read-only
`GET /api/zulip` route.

#918 shipped as PR #484 (`00ebc5a`): six `packages/web/src/<pane>/` modules,
registered `NavId`s, App branches, focused tests, and the frozen NAV list.

---

## 3. Dual-mount → single-mount cutover criteria

`/ui` served `packages/web/dist` when built, else fell back to the legacy
static shell; `/ui/legacy` always served the legacy shell (`spa.py`,
`routes.py`). Cutover executed 2026-09-21 (task #993): `/ui/legacy*` and the
`/ui/excalidraw` alias are gone and a missing dist is a hard 503. **All of the
following had to be true before that retirement PR:**

### C1 — Pane parity (100%)
- [x] All 6 #918 panes shipped as registered `NavId`s in PR #484 (`00ebc5a`).
- [x] `pane-agents` / `pane-activity` folded-parity spot-check signed off.
      `packages/web/tests/sessions.test.tsx` verifies the shared Sessions table
      retains the legacy activity columns and the live activity feed.
- [x] `certification.a11y.spec.ts` frozen NAV list matches all registered nav
      modes, including the six #918 panes; its "covers every registered nav
      mode" assertion is the frozen navigation sign-off.

### C2 — Accessibility gate
- [x] `npm run test:a11y` green: every nav mode, 3 themes (light / dark /
      high-contrast), zero critical/serious axe violations.
      Evidence (task #1125, agent-cursor, 2026-09-19): `packages/web`
      `npm run test:a11y` → **149 passed** after Playwright Chromium install
      (`npx playwright install chromium`; CI uses `--with-deps`).
- [ ] CI `web` job green on `main` (protocol → client → web build, typecheck,
      vitest, build, a11y) — confirmed on the #1125 PR exact-head check;
      main stays Hermes's merge+deploy follow-through.

### C3 — Live data, no fixtures
- [x] Shell header `AgentPresenceStrip` wired to live `/api/agents`
      (`DEMO_AGENTS` removed from `nav.ts`; completed in task #961).
- [x] No remaining `DEMO_*` / prototype fixtures in any shipped pane.

### C4 — Mutation / CSRF / SSE parity
- [x] Every legacy `submitAction` path reachable in modern: approvals, tasks,
      workflows, dispatch, dispatch-stack, dependencies, handoffs,
      autonomous-cycle lifecycle, planning-canvas save, local fleet restart.
      Evidence (#1125): modern SPA posts via `postCsrfJson` /
      pane-specific helpers; dependency form on Tasks detail; handoff +
      Launch next (dispatch-stack) on Dispatch Studio; fleet restart on
      Devices. Regression pins in `hub-csrf.test.ts`, `tasks.test.tsx`,
      `dispatch.test.tsx`, `devices.test.tsx`, `approvals.test.tsx`,
      `cycles.test.tsx`, `canvas.test.tsx`.
- [x] CSRF token refresh-and-retry on a CSRF `403` verified for shared
      `postCsrfJson` and mutating panes (approvals, dependency, handoff,
      fleet).
- [x] SSE error-state recovery (EventSource `error` → reconnecting →
      `cursor` connected on the same mock instance) verified in
      `sessions.test.tsx`. That proves the UI state handler; jsdom does
      not model browser-level EventSource replacement/retry, so C4 does
      not claim that. Panes still refetch via TanStack Query independent
      of event coverage.

### C5 — Edition gating parity
- [x] `UA_EDITION=core` keeps modern `/ui` available while enterprise-only
      Evidence `403` responses render a non-crashing empty state. Covered by
      `tests/test_webui_dual_mount.py::test_modern_spa_remains_available_in_core_edition`,
      `tests/test_evidence_report.py::test_webui_evidence_route_denied_in_core_edition`,
      and `packages/web/tests/evidence.test.tsx`'s 403 state test.

### C6 — Deploy proof on the Mac hub
- [ ] `deploy-mac-hub.sh` web-bundle step builds `packages/web/dist` and ships
      it; `curl http://<hub-ip>:8765/ui` serves the **modern** shell (not
      the fallback), verified after a real deploy.
      **Build evidence (#1125, agent-cursor):** SPA entry
      `assets/index-3Zs6PRSv.js` sha256
      `sha256: 4489035306bf9e9a7bc86de7ddf93647095151a10069933441c64b80b6d2757d`.
      Live re-verify command is in
      `docs/designs/operator-surface-coverage.md` and #1125 notes — Hermes
      runs deploy + curl proof.
- [x] Hard-refresh guidance in `docs/web-ui.md` retained until retirement.

### C7 — Soak + operator sign-off
- [ ] Modern `/ui` is the default for **7 days** with zero P1 UI-regression
      tasks filed against it.
- [ ] the operator's explicit go for retirement (operator sign-off).

### C8 — Retirement PR contents
When C1–C7 hold, one PR:
- [x] Delete routes `/ui/legacy`, `/ui/legacy/{filename}`,
      `/ui/legacy/excalidraw/{path}` and the `serve_legacy_*` helpers in
      `spa.py`.
- [x] Delete `src/unified_agent/webui/static/` (keep the vendored Excalidraw
      bundle only if the modern canvas still loads it).
- [x] Collapse `serve_modern` fallback: dist-missing becomes a hard `503` with
      a build hint (no legacy fallback).
- [x] `docs/web-ui.md` "Legacy app (`/ui/legacy`)" section → one-line
      "removed <date>, see git history at <sha>".
- [x] Drop legacy-only asset tests in `tests/test_webui_assets.py`.

---

## 4. Remaining #885 child tasks

| Child | Scope | State |
|---|---|---|
| #918 | 6 remaining legacy panes → React SPA | done — PR #484 (`00ebc5a`) |
| #890 | Task Queue/Detail pane (unrelated to presence-strip; done, PR #459) | done |
| #961 | live agent-presence strip wiring (C3) | done — PR #498 (`5ebea99`), merged 2026-09-12; `DEMO_AGENTS` removed from `nav.ts`, `AgentStatusBar.tsx` wired to live `/api/agents` via TanStack Query |
| #993 | `/ui/legacy` retirement PR (C8), gated on C1–C7 | in review — branch `task/993-retire-ui-legacy` |

#885 stays **open** until the retirement PR is merged and C1–C7 verified.
Wave 3 (#918) is done as PR #484 (`00ebc5a`); presence-strip wiring (C3) is done as of #961/PR #498 — see the
C3 checklist above.
