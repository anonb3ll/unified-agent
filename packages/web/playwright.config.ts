/*
 * Playwright serves the built app with `vite preview` and tests it.
 *
 * Port: UA_WEB_TEST_PORT (default 4183) sets the preview port for both the
 * server and baseURL. Several worktrees on one machine must each set their
 * own: when UA_WEB_TEST_PORT is set, an existing server on that port is never
 * reused, so a run can only test its own build (a busy port fails loudly
 * instead of silently testing another worktree's dist). Without it, a server
 * already on 4183 is reused outside CI, as before.
 *
 *   UA_WEB_TEST_PORT=4291 npx playwright test tests/app
 *
 * Run `npm run build` first: the preview serves dist/ as it is.
 */
import { defineConfig, devices } from "@playwright/test";

const portFromEnv = process.env.UA_WEB_TEST_PORT;
if (portFromEnv !== undefined && !/^\d{2,5}$/.test(portFromEnv)) {
  throw new Error(`UA_WEB_TEST_PORT must be a port number, got "${portFromEnv}"`);
}
const port = Number(portFromEnv ?? 4183);
const origin = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: ["**/*.a11y.spec.ts", "**/*.e2e.spec.ts"],
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: `${origin}/ui/`,
    trace: "on-first-retry",
  },
  webServer: {
    command: `npm run preview -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${origin}/ui/`,
    reuseExistingServer: !process.env.CI && portFromEnv === undefined,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
