// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

async function load(port?: string) {
  vi.resetModules();
  if (port === undefined) delete process.env.UA_WEB_TEST_PORT;
  else process.env.UA_WEB_TEST_PORT = port;
  return (await import("../../playwright.config")).default;
}

afterEach(() => {
  delete process.env.UA_WEB_TEST_PORT;
});

describe("playwright.config port", () => {
  it("defaults to 4183", async () => {
    const config = await load();
    expect(config.use?.baseURL).toBe("http://127.0.0.1:4183/ui/");
    const server = config.webServer as { command: string; url: string; reuseExistingServer: boolean };
    expect(server.command).toContain("--port 4183");
    expect(server.url).toBe("http://127.0.0.1:4183/ui/");
  });

  it("UA_WEB_TEST_PORT moves server and baseURL and never reuses a server", async () => {
    const config = await load("4291");
    const server = config.webServer as { command: string; url: string; reuseExistingServer: boolean };
    expect(config.use?.baseURL).toBe("http://127.0.0.1:4291/ui/");
    expect(server.command).toContain("--port 4291 --strictPort");
    expect(server.url).toBe("http://127.0.0.1:4291/ui/");
    expect(server.reuseExistingServer).toBe(false);
  });

  it("refuses a value that is not a port", async () => {
    await expect(load("4291; rm -rf /")).rejects.toThrow(/must be a port number/);
  });
});
