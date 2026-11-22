import { afterEach, describe, expect, it, vi } from "vitest";
import { DENSITY_KEY } from "../../../src/app/settings/appearance";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import { settingsHub } from "./hub";

// Its own file: the Settings routes module must not have been imported yet.
vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  delete document.documentElement.dataset.density;
});

describe("settings: stored appearance (CTO review)", () => {
  it("applies the stored density when the area mounts, not when its module is imported", async () => {
    // Stored by this owner on an earlier visit.
    expect(fixtureEpoch().sharedStorage.write(DENSITY_KEY, "compact")).toBe(true);
    delete document.documentElement.dataset.density;
    await import("../../../src/app/settings/routes");
    expect(document.documentElement.dataset.density).toBeUndefined();
    renderRoute("/settings/health", { hub: settingsHub() });
    await findPageHeading("Health");
    expect(document.documentElement.dataset.density).toBe("compact");
  });
});
