# @unified-agent/web

React 19 + Vite Control Center SPA. Since the Control Center v2 cutover
(task #2479) `/ui/` is the eight-destination app in `src/app/`, built to
[`docs/designs/control-center-v2-spec.md`](../../docs/designs/control-center-v2-spec.md).
Operator and route documentation: [`docs/web-ui.md`](../../docs/web-ui.md).

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server (base `/ui/`) |
| `npm run build` | `tsc --noEmit` typecheck + production bundle → `dist/` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest (jsdom) |
| `npm run test:a11y` | Playwright + `@axe-core/playwright` — WCAG 2.2 AA gate (`wcag2a wcag2aa wcag21a wcag21aa wcag22aa`), zero violations on every route × light/dark/high-contrast (`tests/app/shell.a11y.spec.ts`, `certification.a11y.spec.ts`) plus each area's `tests/app/<area>.a11y.spec.ts` and `.e2e.spec.ts` |
| `npm run preview` | Serve the built `dist/` (a11y specs run against `http://127.0.0.1:4183/ui/`) |

CI runs all of these in the `web` job of [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
(protocol → client → web build, typecheck, Vitest, build, Playwright a11y gate).

## Developing against a live hub

`npm run dev` serves the SPA on loopback (`http://127.0.0.1:5173/ui/`) and, on
its own, proxies nothing. To see live hub data, point it at a hub explicitly:

```bash
UA_DEV_HUB_URL=http://100.64.0.10:8765 npm run dev
```

`/api`, `/healthz`, `/oauth` and `/mcp` are then proxied to that hub's origin
(`vite.config.ts`, `dev-proxy.ts`, task #1579). The SPA keeps calling them
same-origin, exactly as it does when the hub serves `dist/`.

- **Topology.** The Mac hub runs the only hub and every other machine is a
  client of it, so `UA_DEV_HUB_URL` names that hub over Tailscale. The desktop
  never runs a hub of its own. A hub on loopback is only an isolated test
  fixture with a throwaway `UA_HOME`, never a second production hub.
- **Loopback only.** The proxy carries your hub session cookie and the OAuth
  sign-in, so the dev server binds to `127.0.0.1`. With `UA_DEV_HUB_URL` set,
  `--host` (or any non-loopback `server.host`) is refused at startup.
- **No credentials in the URL.** A `user:password@` URL is refused. Sign in
  through the UI. Mutations need the CSRF token from `/api/whoami` as usual.
- Only `http` and `https` hub URLs are accepted. The path is ignored; the
  proxy targets the hub's origin.

## Design system

`npm run design-system` rebuilds the Unified Agent design system (the claude.ai
Design System artifact) into `design-system/out/project/`, which is gitignored.
Values the code owns are read from the code: theme colours from `src/index.css`,
persona hues from `src/lib/ui/AgentPersonaBadge.tsx`, the `src/lib/ui` primitives bundled
as one IIFE (`window.UnifiedAgent`, React from the page) with the Tailwind
utilities they use in `bundle.css`, and types emitted from the same files.
Everything people write lives here and is edited by hand:

| Path | Holds |
|---|---|
| `design-system/tokens.meta.json` | usage notes, themes, type styles, spacing, radii, sizes, opacity |
| `design-system/content/README.md` | the brand book |
| `design-system/content/components/<Comp>/` | each component's guide and live preview; `Cover/` is the cover |

The build fails when `src/index.css` gains, loses or renames a `--ua-*` colour,
or a persona disappears, until `tokens.meta.json` is updated to match. To
publish, ask Claude Code to re-sync the design system from that folder; it
uploads the changed files to the artifact and keeps the artifact's own index.
`components/index.d.ts` must be sent as `text/plain`.

## Layout

| Path | Holds |
| --- | --- |
| `src/main.tsx` | Entry: rewrites classic `/ui/?view=<id>` and `/ui/#<id>` links (`src/app/lib/legacyRedirect.ts`, spec §11), then mounts the app |
| `src/app/` | The app: `router.tsx`, `routes.ts` (the eight destinations and every route, spec §3), `shell/`, `lib/`, one directory per area (`home`, `inbox`, `work`, `agents`, `sessions`, `knowledge`, `runs`, `settings`, `integrations`) |
| `src/lib/` | Code shared below the app: hub fetchers (`api/`, `hub.ts`), polling and the event stream, the theme, the sign-in view, and the design-system primitives (`ui/`) |
| `tests/app/` | Vitest per area, Playwright `<area>.e2e.spec.ts` and `<area>.a11y.spec.ts` with the hub mocked |
| `tests/nav.test.ts` | The destinations, routes and legacy view map, checked against the spec text |

`/ui/?showcase=primitives` renders every primitive for `tests/ui.a11y.spec.ts`.

## Data model

- **TanStack Query** for every read (`staleTime` 5s, no refetch-on-focus),
  same-origin cookie `fetch` to the hub's `/api`. Fetchers, query keys and
  response types live in `src/lib/api/<domain>.ts` (shared) or
  `src/app/<area>/api.ts` (one area); `src/lib/hub.ts` re-exports the shared ones.
- **SSE**: one `EventSource('/api/events/stream')` per tab, shared through
  `subscribeEventStream`. Subscriber `?channels=` filters apply locally (exact
  names or trailing `*` prefixes); the hub still enforces authenticated read
  scope. Pages use `useStreamInvalidation` with their relevant channels to
  coalesce refreshes and defer them while hidden. Evidence, telemetry and
  activity views intentionally receive the full feed. `cursor` means
  connected/heartbeat; `error` means reconnecting. The last unsubscribe closes
  the transport and cancels refusal retries. Historical reads keep their own
  cursors; subscribing a new page does not replay prior messages.
- **CSRF**: mutations read `x-ua-csrf` from `GET /api/whoami`, with a one-shot
  refresh + retry on a CSRF `403`.

## Dependencies

- `@unified-agent/client` / `@unified-agent/protocol` via `file:../…` (build those first).
- Vite `base: '/ui/'`, `resolve.dedupe: ['react','react-dom']`, workspace
  `server.fs.allow` per research #886.

## Serving

Starlette single-mount (`webui/spa.py`, `web_routes()`): `/ui` serves this
package's `dist/` when `dist/index.html` exists, otherwise the hub answers a
hard 503 with a build hint — the legacy vanilla UI was retired in task #993.
The cockpit `scripts/deploy-mac-hub.sh` builds and ships `dist/` to the hub by
default; `UA_SKIP_WEB_BUILD=1` opts out.
