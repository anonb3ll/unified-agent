import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { groupChecks, plainName, statusOf } from "../../../src/app/settings/HealthPage";
import { commands, routes } from "../../../src/app/settings/routes";
import { CLOUD, HEALTH, POOL_UNAVAILABLE, QUOTA_UNAVAILABLE } from "./fixtures";
import { settingsHub } from "./hub";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

describe("settings: routes", () => {
  it("redirects /settings to Health and keeps the integrations mount", async () => {
    const { router } = renderRoute("/settings", { hub: settingsHub() });
    await findPageHeading("Health");
    expect(router.state.location.pathname).toBe("/ui/settings/health");
    expect(routes[0].children?.find((r) => r.path === "integrations")?.children?.length).toBeGreaterThan(0);
  });

  it("exports palette commands for every settings page", () => {
    const targets = commands.map((c) => c.to);
    for (const path of ["/settings/health", "/settings/platform", "/settings/cloud", "/settings/usage", "/settings/credentials", "/settings/audit", "/settings/audit/provenance", "/settings/appearance"]) {
      expect(targets).toContain(path);
    }
  });
});

describe("settings: health", () => {
  it("groups checks in plain words with what to do, raw state secondary", async () => {
    renderRoute("/settings/health", { hub: settingsHub() });
    await findPageHeading("Health");
    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("Workflow definitions")).toBeInTheDocument();
    expect(within(attention).getByText("Meaning-based memory search")).toBeInTheDocument();
    expect(within(attention).getByText("Set up wrong")).toBeInTheDocument();
    expect(within(attention).getAllByText(/What to do:/).length).toBe(3);
    const working = screen.getByRole("region", { name: "Working" });
    expect(within(working).getByText("Sign-in and access tokens")).toBeInTheDocument();
    const off = screen.getByRole("region", { name: "Off" });
    expect(within(off).getByText("Zulip chat")).toBeInTheDocument();
    // Raw name present, but not as the heading.
    expect(within(attention).getByText("MISCONFIGURED").tagName).toBe("CODE");
    expect(screen.getByText(/^Checked \d+s ago$/)).toBeInTheDocument();
  });

  it("orders needs-attention by severity and names unknown checks", () => {
    const groups = groupChecks(HEALTH.checks);
    expect(groups.attention.map(statusOf)).toEqual(["MISCONFIGURED", "DEGRADED", "UNKNOWN"]);
    expect(plainName({ capability: "some_new_probe" })).toBe("Some new probe");
  });

  it("runs the checks again on demand with force=1", async () => {
    const urls: string[] = [];
    renderRoute("/settings/health", {
      hub: settingsHub({
        "GET /api/health": ({ url }) => {
          urls.push(url);
          return { body: HEALTH };
        },
      }),
    });
    await screen.findByRole("region", { name: "Working" });
    fireEvent.click(screen.getByRole("button", { name: "Run the checks now" }));
    await waitFor(() => expect(urls.some((u) => u.includes("force=1"))).toBe(true));
  });

  it("explains a failed read in plain words, raw text only in the detail (defect 10)", async () => {
    renderRoute("/settings/health", { hub: settingsHub({ "GET /api/health": { status: 500, body: { error: "sqlite3.OperationalError: database is locked" } } }) });
    await findPageHeading("Health");
    expect(await screen.findByText("The health report could not be loaded")).toBeInTheDocument();
    expect(screen.getByText(/The hub had a problem answering/)).toBeInTheDocument();
    expect(screen.getByText(/database is locked/).closest("details")).not.toBeNull();
    expect(screen.queryByText("Hub totals", { exact: false })).not.toBeInTheDocument();
  });

  it("shows no totals while the report is loading (defect 4)", async () => {
    renderRoute("/settings/health", { hub: settingsHub({ "GET /api/health": () => new Promise(() => {}) }) });
    await findPageHeading("Health");
    expect(screen.queryByText(/Hub totals/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });
});

describe("settings: platform, cloud, usage", () => {
  it("lists object kinds and the harness configuration", async () => {
    renderRoute("/settings/platform", { hub: settingsHub() });
    await findPageHeading("Platform");
    expect(await screen.findByRole("table", { name: "Object kinds the platform owns" })).toBeInTheDocument();
    expect(screen.getByText("open → doing → review → done")).toBeInTheDocument();
    expect(await screen.findByText("Version 7")).toBeInTheDocument();
  });

  it("says a cloud reading is partial and never softens an unreadable instance", async () => {
    renderRoute("/settings/cloud", { hub: settingsHub() });
    await findPageHeading("Cloud");
    expect(await screen.findByText("This is a partial reading.")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("budgets")).toBeInTheDocument();
  });

  it("shows unknown instance state as could not read", async () => {
    renderRoute("/settings/cloud", { hub: settingsHub({ "GET /api/cloud": { body: { ...CLOUD, instance_state: "unknown" } } }) });
    expect(await screen.findAllByText("Could not read")).not.toHaveLength(0);
  });

  it("treats a 200 that names an error as a failed read", async () => {
    renderRoute("/settings/usage", { hub: settingsHub({ "GET /api/quota": { body: QUOTA_UNAVAILABLE } }) });
    await findPageHeading("Usage");
    expect(await screen.findByText("Model availability could not be loaded")).toBeInTheDocument();
    // The other sections still load.
    expect(await screen.findByText("monthly-api")).toBeInTheDocument();
    expect(screen.getByText("Misses the target")).toBeInTheDocument();
    expect(screen.getByText("No data")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("keeps data on a failed refetch with Retry and its age", async () => {
    let calls = 0;
    const { appClient } = renderRoute("/settings/cloud", {
      hub: settingsHub({
        "GET /api/cloud": () => {
          calls += 1;
          return calls === 1 ? { body: CLOUD } : { status: 503, body: { error: "gcloud down" } };
        },
      }),
    });
    await screen.findByText("Running");
    await appClient().refetchQueries({ queryKey: ["settings", "cloud"] });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not refresh the cloud project/);
    expect(alert).toHaveTextContent(/may be out of date/);
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });
});

describe("settings: credentials pool", () => {
  it("shows present or missing per provider and never a value", async () => {
    const { container } = renderRoute("/settings/credentials", { hub: settingsHub() });
    await findPageHeading("Credentials");
    expect(await screen.findByText("Present")).toBeInTheDocument();
    expect(screen.getByText("Missing")).toBeInTheDocument();
    expect(container.querySelector('[data-provider="anthropic"]')).toHaveAttribute("data-present", "true");
    expect(container.querySelector('[data-provider="openai"]')).toHaveAttribute("data-present", "false");
  });

  it("names an unavailable pool as a failure, not an empty registry", async () => {
    renderRoute("/settings/credentials", { hub: settingsHub({ "GET /api/credential_pool": { body: POOL_UNAVAILABLE } }) });
    expect(await screen.findByText("Provider credentials could not be loaded")).toBeInTheDocument();
    expect(screen.queryByText("No provider credentials")).not.toBeInTheDocument();
  });
});
