import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { whoamiFixture } from "../support/fixtures";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import {
  CONNECTORS,
  CONNECTORS_EMPTY,
  CONNECTORS_UNAVAILABLE,
  OAUTH_BEGIN,
  OAUTH_CONNECTED,
  OAUTH_DISCONNECTED,
  OAUTH_NEEDS_RECONNECT,
  OAUTH_STATE,
  parkedBody,
} from "./fixtures";
import { integrationsHub, LOST, posts } from "./hub";

const leave = vi.hoisted(() => vi.fn());
vi.mock("../../../src/app/integrations/navigate", () => ({ leaveForProvider: leave }));

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
  leave.mockReset();
});

const PAGE = "/settings/integrations";

describe("integrations: connected accounts", () => {
  it("shows the GitHub connection in plain words with its scopes", async () => {
    renderRoute(PAGE, { hub: integrationsHub() });
    await findPageHeading("Integrations");
    expect(await screen.findByTestId("oauth-github-status")).toHaveTextContent("Connected");
    expect(screen.getByText(/Access granted: read:user, repo/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconnect GitHub" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Disconnect GitHub" })).toBeEnabled();
  });

  it("a disconnected account offers Connect and no Disconnect", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "GET /api/connectors/oauth/github": { body: OAUTH_DISCONNECTED } }) });
    expect(await screen.findByTestId("oauth-github-status")).toHaveTextContent("Not connected");
    expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect GitHub" })).not.toBeInTheDocument();
  });

  it("needs_reconnect says what happened and offers Reconnect", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "GET /api/connectors/oauth/github": { body: OAUTH_NEEDS_RECONNECT } }) });
    expect(await screen.findByTestId("oauth-github-status")).toHaveTextContent("Needs reconnecting");
    expect(screen.getByText(/The grant stopped working/)).toBeInTheDocument();
  });

  it("a 403 explains that only the human operator manages accounts", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({ "GET /api/connectors/oauth/github": { status: 403, body: { error: "connector accounts require a human operator" } } }),
    });
    expect(await screen.findByRole("heading", { name: /Only the human operator can manage connected accounts/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Connect GitHub/ })).not.toBeInTheDocument();
  });

  it("connect explains where the person goes, then leaves for github.com without showing the sign-in address", async () => {
    const { hub, router } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/connectors/oauth/github": { body: OAUTH_DISCONNECTED },
        "POST /api/connectors/oauth/github/begin": { status: 201, body: OAUTH_BEGIN },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub?" });
    expect(within(dialog).getByText("You will go to github.com")).toBeInTheDocument();
    expect(within(dialog).getByText(/short confirmation page on this hub/)).toBeInTheDocument();
    expect(posts(hub.calls, "/api/connectors/oauth/github/begin")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue to github.com" }));
    await waitFor(() => expect(leave).toHaveBeenCalledTimes(1));
    expect(leave).toHaveBeenCalledWith(OAUTH_BEGIN.authorize_url);
    expect(posts(hub.calls, "/api/connectors/oauth/github/begin")[0].headers["x-ua-csrf"]).toBe("csrf-1");
    expect(document.body.innerHTML).not.toContain(OAUTH_STATE);
    // The one-use sign-in address is never kept: not in storage, the URL or a draft.
    const stored = (store: Storage) => Array.from({ length: store.length }, (_, i) => `${store.key(i)}=${store.getItem(store.key(i) ?? "")}`).join("\n");
    expect(stored(sessionStorage)).not.toContain(OAUTH_STATE);
    expect(stored(localStorage)).not.toContain(OAUTH_STATE);
    expect(router.state.location.search + router.state.location.hash).not.toContain(OAUTH_STATE);
  });

  it("a sign-in started by one owner never sends the next owner's page to the provider", async () => {
    let release: (reply: { status: number; body: unknown }) => void = () => {};
    const answer = new Promise<{ status: number; body: unknown }>((resolve) => (release = resolve));
    let agent = "operator";
    const { queryClient, epoch, hub } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/whoami": () => ({ body: whoamiFixture({ agent }) }),
        "GET /api/connectors/oauth/github": { body: OAUTH_DISCONNECTED },
        "POST /api/connectors/oauth/github/begin": () => answer,
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue to github.com" }));
    await waitFor(() => expect(posts(hub.calls, "/api/connectors/oauth/github/begin")).toHaveLength(1));
    agent = "Desktop_Agent2";
    await act(async () => {
      queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent }));
    });
    await waitFor(() => expect(epoch().owner?.identity).toBe("Desktop_Agent2"));
    release({ status: 201, body: OAUTH_BEGIN });
    await new Promise((r) => setTimeout(r, 100));
    expect(leave).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a sign-in cancelled while it was starting never leaves the page when its answer arrives", async () => {
    let release: (reply: { status: number; body: unknown }) => void = () => {};
    const answer = new Promise<{ status: number; body: unknown }>((resolve) => (release = resolve));
    const { hub } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/connectors/oauth/github": { body: OAUTH_DISCONNECTED },
        "POST /api/connectors/oauth/github/begin": () => answer,
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue to github.com" }));
    await waitFor(() => expect(posts(hub.calls, "/api/connectors/oauth/github/begin")).toHaveLength(1));
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    release({ status: 201, body: OAUTH_BEGIN });
    await new Promise((r) => setTimeout(r, 100));
    expect(leave).not.toHaveBeenCalled();
  });

  it("a disconnect answered after its dialog was closed and opened again never closes the new one", async () => {
    let release: (reply: { status?: number; body: unknown }) => void = () => {};
    const answer = new Promise<{ status?: number; body: unknown }>((resolve) => (release = resolve));
    const { hub } = renderRoute(PAGE, { hub: integrationsHub({ "POST /api/connectors/oauth/github/disconnect": () => answer }) });
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect GitHub" }));
    const first = await screen.findByRole("dialog", { name: "Disconnect GitHub?" });
    fireEvent.click(within(first).getByRole("button", { name: "Disconnect GitHub" }));
    await waitFor(() => expect(posts(hub.calls, "/api/connectors/oauth/github/disconnect")).toHaveLength(1));
    fireEvent.keyDown(first, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Disconnect GitHub" }));
    const second = await screen.findByRole("dialog", { name: "Disconnect GitHub?" });
    release({ body: OAUTH_DISCONNECTED });
    expect(await screen.findByText("GitHub is disconnected.")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByRole("dialog", { name: "Disconnect GitHub?" })).toBe(second);
  });

  it("refuses an authorize address that is not https on github.com, and opens nothing", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({
        "POST /api/connectors/oauth/github/begin": {
          status: 201,
          body: { provider: "github", authorize_url: `https://evil.example/login?state=${OAUTH_STATE}` },
        },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Reconnect GitHub" }));
    const dialog = await screen.findByRole("dialog", { name: "Reconnect GitHub?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue to github.com" }));
    expect(await within(dialog).findByText(/points to evil.example, not github.com as expected. Nothing was opened./)).toBeInTheDocument();
    expect(leave).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain(OAUTH_STATE);
  });

  it("a hub refusal is explained in plain words, with the raw text collapsed", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({
        "POST /api/connectors/oauth/github/begin": { status: 400, body: { error: "connector OAuth requires a configured HTTPS hub issuer" } },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Reconnect GitHub" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue to github.com" }));
    expect(await within(dialog).findByRole("heading", { name: /GitHub was not opened/ })).toBeInTheDocument();
    expect(within(dialog).getByText(/The hub did not accept this/)).toBeInTheDocument();
    expect(leave).not.toHaveBeenCalled();
  });

  it("disconnect confirms by naming the account and what stops working, then refreshes the status", async () => {
    let status = OAUTH_CONNECTED as Record<string, unknown>;
    const { hub } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/connectors/oauth/github": () => ({ body: status }),
        "POST /api/connectors/oauth/github/disconnect": () => {
          status = OAUTH_DISCONNECTED;
          return { body: OAUTH_DISCONNECTED };
        },
      }),
    });
    const trigger = await screen.findByRole("button", { name: "Disconnect GitHub" });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Disconnect GitHub?" });
    expect(within(dialog).getByText("GitHub connected account")).toBeInTheDocument();
    expect(within(dialog).getByText(/stop working until someone connects/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect GitHub" }));
    await waitFor(() => expect(screen.getByTestId("oauth-github-status")).toHaveTextContent("Not connected"));
    expect(posts(hub.calls, "/api/connectors/oauth/github/disconnect")).toHaveLength(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a parked disconnect is shown as waiting for approval, never as done", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({ "POST /api/connectors/oauth/github/disconnect": { status: 202, body: parkedBody("connector_oauth_disconnect", 91) } }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect GitHub" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect GitHub" }));
    expect(await within(dialog).findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(await within(dialog).findByText(/submit this form again/)).toBeInTheDocument();
    expect(screen.queryByText("GitHub is disconnected.")).not.toBeInTheDocument();
    expect(screen.getByTestId("oauth-github-status")).toHaveTextContent("Connected");
  });

  it("a lost answer says it may have been disconnected and blocks a blind retry", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "POST /api/connectors/oauth/github/disconnect": LOST }) });
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect GitHub" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect GitHub" }));
    expect(await within(dialog).findByRole("heading", { name: "It may have been disconnected" })).toBeInTheDocument();
    expect(within(dialog).getByText(/check the status above before trying again/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Disconnect GitHub" })).toBeDisabled();
  });
  it("a failed re-check never leaves Connected on screen as current", async () => {
    let fail = false;
    const { appClient } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/connectors/oauth/github": () => (fail ? { status: 503, body: { error: "busy" } } : { body: OAUTH_CONNECTED }),
      }),
    });
    expect(await screen.findByTestId("oauth-github-status")).toHaveTextContent("Connected");
    fail = true;
    await appClient().refetchQueries({ queryKey: ["integrations", "oauth", "github"] }).catch(() => undefined);
    expect(await screen.findByRole("heading", { name: "Could not refresh the GitHub connection" })).toBeInTheDocument();
    expect(screen.getByTestId("oauth-github-status")).toHaveTextContent("Last known: Connected");
    expect(screen.getByText(/The latest check failed/)).toBeInTheDocument();
    expect(screen.queryByText(/The hub holds a working grant/)).not.toBeInTheDocument();
  });
  it("a refetch never changes what an open connect dialog does", async () => {
    let status: unknown = OAUTH_DISCONNECTED;
    const { appClient } = renderRoute(PAGE, { hub: integrationsHub({ "GET /api/connectors/oauth/github": () => ({ body: status }) }) });
    fireEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub?" });
    status = OAUTH_CONNECTED;
    await appClient().refetchQueries({ queryKey: ["integrations", "oauth", "github"] });
    await waitFor(() => expect(screen.getByTestId("oauth-github-status")).toHaveTextContent("Connected"));
    expect(dialog).toHaveAccessibleName("Connect GitHub?");
  });
});

describe("integrations: connector registry", () => {
  it("lists packages and servers with plain status, and counts from the complete read", async () => {
    renderRoute(PAGE, { hub: integrationsHub() });
    expect(await screen.findByTestId("package-status-github")).toHaveTextContent("Working");
    expect(screen.getByTestId("package-status-int-gcp-agent20")).toHaveTextContent("Not set up");
    expect(screen.getByText("Missing credentials: GCP_SERVICE_ACCOUNT")).toBeInTheDocument();
    expect(screen.getByTestId("server-status-linear-old")).toHaveTextContent("Revoked");
    expect(screen.getByText(/Packages: 2\. Registered servers: 2\./)).toBeInTheDocument();
    expect(screen.getByText(/Horizon-2 is the hub's reviewed connector registry/)).toBeInTheDocument();
    // Everything the classic pane showed is still here.
    expect(screen.getByText("Version 1.4.0")).toBeInTheDocument();
    expect(screen.getByText("From packaged:int-github@1.4.0")).toBeInTheDocument();
    expect(screen.getAllByText("one-shot approval").length).toBeGreaterThan(0);
    expect(screen.getByText("GCP runtime health DORMANT: no project configured")).not.toBeVisible();
  });

  it("register, authorize and revoke are offered for live servers only, and nothing is sent until asked", async () => {
    const { hub } = renderRoute(PAGE, { hub: integrationsHub() });
    await screen.findByTestId("server-status-github-main");
    expect(screen.getByRole("button", { name: "Register a connector" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Authorize github-main" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Revoke github-main" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Revoke linear-old" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Not available on this hub yet/)).not.toBeInTheDocument();
    expect(hub.calls.some((c) => c.path.startsWith("/api/action/connector"))).toBe(false);
  });

  it("an unavailable registry is an error with Retry and counts show –, never 0", async () => {
    let body: unknown = CONNECTORS_UNAVAILABLE;
    renderRoute(PAGE, { hub: integrationsHub({ "GET /api/connectors": () => ({ body }) }) });
    expect(await screen.findByRole("heading", { name: "Could not load the connector registry" })).toBeInTheDocument();
    expect(screen.getByText(/Packages: –\. Registered servers: –\./)).toBeInTheDocument();
    expect(screen.queryByText(/horizon2 unavailable/)).not.toBeVisible();
    body = CONNECTORS;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("package-status-github")).toBeInTheDocument();
  });

  it("an empty registry says so", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "GET /api/connectors": { body: CONNECTORS_EMPTY } }) });
    expect(await screen.findByRole("heading", { name: "No connectors yet" })).toBeInTheDocument();
    expect(screen.getByText(/Packages: 0\. Registered servers: 0\./)).toBeInTheDocument();
  });

  it("a failed refresh keeps the data, shows the error with Retry and how old the data is", async () => {
    let fail = false;
    const { appClient } = renderRoute(PAGE, {
      hub: integrationsHub({ "GET /api/connectors": () => (fail ? { status: 503, body: { error: "busy" } } : { body: CONNECTORS }) }),
    });
    await screen.findByTestId("package-status-github");
    fail = true;
    await appClient().refetchQueries({ queryKey: ["integrations", "connectors"] }).catch(() => undefined);
    expect(await screen.findByRole("heading", { name: "Could not refresh the connector registry" })).toBeInTheDocument();
    expect(screen.getByText(/may be out of date/)).toBeInTheDocument();
    // The old result is never shown as current.
    expect(screen.getByTestId("package-status-github")).toHaveTextContent("Last known: Working");
    expect(screen.getByTestId("package-status-github")).toHaveAttribute("data-stale", "true");
    expect(screen.getByText(/Packages: –/)).toBeInTheDocument();
  });
});
