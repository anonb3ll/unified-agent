import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { areaOf } from "../../src/app/router";
import { DESTINATIONS, ROUTE_REDIRECTS, ROUTE_SAMPLES } from "../../src/app/routes";
import { findPageHeading, renderRoute } from "./support/render";

afterEach(() => vi.unstubAllGlobals());

describe("router", () => {
  it("covers every destination with a route sample", () => {
    for (const d of DESTINATIONS) {
      expect(ROUTE_SAMPLES.some((r) => r.path === d.path || r.path.startsWith(`${d.path}/`))).toBe(true);
    }
  });

  it.each(ROUTE_SAMPLES.map((r) => [r.path, r.title] as const))("%s resolves to its page", async (path, title) => {
    const { router } = renderRoute(path);
    expect(await findPageHeading(title)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Page not found" })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/ui${path}`);
    await waitFor(() => expect(document.title).toBe(`${title} · Control Center`));
  });

  it.each(ROUTE_REDIRECTS.map((r) => [r.from, r.to] as const))("%s redirects to %s", async (from, to) => {
    const { router } = renderRoute(from);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/ui${to}`));
  });

  it.each(["/nowhere", "/work/nowhere/deeper", "/agents/Desktop_Agent1"])("%s shows not found with the eight destinations", async (path) => {
    renderRoute(path);
    expect(await findPageHeading("Page not found")).toBeInTheDocument();
    for (const d of DESTINATIONS) {
      expect(screen.getByRole("main").querySelector(`a[href="/ui${d.path}"]`)).not.toBeNull();
    }
  });

  it("areaOf reads the first segment with or without the basename", () => {
    expect(areaOf("/ui/work/goals")).toBe("work");
    expect(areaOf("/settings/integrations/slack")).toBe("settings");
    expect(areaOf("/ui/integrations")).toBeUndefined();
    expect(areaOf("/ui/")).toBeUndefined();
  });
});

describe("area discovery survives a cancelled navigation (round 3, item 1)", () => {
  it("leaving an area before its import finishes does not make it not-found later", async () => {
    const { createMemoryRouter } = await import("react-router");
    const { appRoutes, createAreaPatcher, AREA_LOADERS } = await import("../../src/app/router");
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const loaders = {
      ...AREA_LOADERS,
      work: async () => {
        await gate;
        return AREA_LOADERS.work();
      },
    };
    mockFetchShell();
    const router = createMemoryRouter(appRoutes(), {
      basename: "/ui",
      initialEntries: ["/ui/home"],
      patchRoutesOnNavigation: createAreaPatcher(loaders),
    });
    await waitFor(() => expect(router.state.matches.some((m) => m.route.id === "area-home")).toBe(true));
    const toWork = router.navigate("/work");
    // Leave before the Work import finishes: the first navigation is cancelled.
    await router.navigate("/home");
    release();
    await toWork;
    await router.navigate("/work");
    expect(router.state.location.pathname).toBe("/ui/work");
    expect(router.state.matches.some((m) => m.route.id === "area-work")).toBe(true);
    router.dispose();
  });
});

function mockFetchShell() {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
}
