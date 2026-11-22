import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import {
  NOTIFY_EMPTY,
  NOTIFY_FULL,
  NOTIFY_UNAVAILABLE,
  SLACK_DORMANT,
  SLACK_LIVE,
  SLACK_MISCONFIGURED,
  SLACK_UNAVAILABLE,
  ZULIP_EMPTY,
  ZULIP_INBOUND_FAILED,
  ZULIP_UNCONFIGURED,
} from "./fixtures";
import { integrationsHub } from "./hub";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("integrations: Slack", () => {
  it("shows a working gateway, its allowlists and credential presence, never a value", async () => {
    renderRoute("/settings/integrations/slack", { hub: integrationsHub() });
    await findPageHeading("Slack");
    expect(await screen.findByTestId("slack-status")).toHaveTextContent("Working");
    expect(screen.getByTestId("slack-channels")).toHaveTextContent("3");
    expect(screen.getByTestId("slack-signing")).toHaveTextContent("Present");
    expect(screen.getByTestId("slack-bot")).toHaveTextContent("Present");
    expect(screen.getByText("Desktop_Agent5")).toBeInTheDocument();
    expect(screen.getByTestId("slack-config-pointer")).toHaveTextContent(/Change them on the hub and restart it/);
    // Recent activity in plain words, with a failure called out.
    expect(screen.getByText("Reply failed")).toBeInTheDocument();
    expect(screen.getByText("Message received")).toBeInTheDocument();
    expect(screen.getByText("Some recent messages did not get through")).toBeInTheDocument();
    expect(screen.getByText(/Only the last 20 are read/)).toBeInTheDocument();
  });

  it("a misconfigured gateway says what to fix for each missing piece", async () => {
    renderRoute("/settings/integrations/slack", { hub: integrationsHub({ "GET /api/slack_gateway": { body: SLACK_MISCONFIGURED } }) });
    expect(await screen.findByTestId("slack-status")).toHaveTextContent("Set-up incomplete");
    const reasons = screen.getByTestId("slack-reasons");
    expect(within(reasons).getByText(/cannot check that messages really come from Slack/)).toBeInTheDocument();
    expect(within(reasons).getByText(/can read messages but cannot reply/)).toBeInTheDocument();
    expect(screen.getByTestId("slack-signing")).toHaveTextContent("Missing");
    expect(screen.getByText("No Slack activity has been recorded.")).toBeInTheDocument();
  });

  it("an off gateway says how to turn it on", async () => {
    renderRoute("/settings/integrations/slack", { hub: integrationsHub({ "GET /api/slack_gateway": { body: SLACK_DORMANT } }) });
    expect(await screen.findByTestId("slack-status")).toHaveTextContent("Off");
    expect(screen.getByText("How to turn it on")).toBeInTheDocument();
  });

  it("an unavailable status is an error with Retry, the hub's text collapsed", async () => {
    let body: unknown = SLACK_UNAVAILABLE;
    renderRoute("/settings/integrations/slack", { hub: integrationsHub({ "GET /api/slack_gateway": () => ({ body }) }) });
    expect(await screen.findByRole("heading", { name: "Could not load the Slack status" })).toBeInTheDocument();
    expect(screen.getByText("Technical detail")).toBeInTheDocument();
    body = SLACK_LIVE;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("slack-status")).toHaveTextContent("Working");
  });
  it("a failed refresh shows Last known, never Working as current", async () => {
    let fail = false;
    const { appClient } = renderRoute("/settings/integrations/slack", {
      hub: integrationsHub({ "GET /api/slack_gateway": () => (fail ? { status: 500, body: { error: "boom" } } : { body: SLACK_LIVE }) }),
    });
    expect(await screen.findByTestId("slack-status")).toHaveTextContent("Working");
    fail = true;
    await appClient().refetchQueries({ queryKey: ["integrations", "slack"] }).catch(() => undefined);
    expect(await screen.findByRole("heading", { name: "Could not refresh the Slack status" })).toBeInTheDocument();
    expect(screen.getByTestId("slack-status")).toHaveTextContent("Last known: Working");
    expect(screen.queryByText(/The hub accepts messages/)).not.toBeInTheDocument();
  });
});

describe("integrations: Zulip", () => {
  it("shows worker health, channels and recent messages, with task links and only safe external links", async () => {
    renderRoute("/settings/integrations/zulip", { hub: integrationsHub() });
    await findPageHeading("Zulip");
    expect(await screen.findByTestId("zulip-status")).toHaveTextContent("Failing");
    expect(screen.getByText(/a wrong bot key or an unreachable Zulip site/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /zulip\.example\.test/ })).toHaveAttribute("href", "https://zulip.example.test/");
    expect(screen.getByTestId("zulip-inbound-24h")).toHaveTextContent("2");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Task 2478" })).toHaveAttribute("href", "/ui/work/tasks/2478");
    const links = screen.getAllByRole("link", { name: /In Zulip/ });
    expect(links).toHaveLength(1);
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(screen.getByText("A bot (no action)")).toBeInTheDocument();
    expect(screen.getByText(/Only the last 30 are shown/)).toBeInTheDocument();
    expect(screen.getAllByText("Up to message 9001")).toHaveLength(2);
  });

  it("an unconfigured site says it is not set up and how, without empty tables", async () => {
    renderRoute("/settings/integrations/zulip", { hub: integrationsHub({ "GET /api/zulip": { body: ZULIP_UNCONFIGURED } }) });
    expect(await screen.findByTestId("zulip-status")).toHaveTextContent("Not set up");
    expect(screen.getByText("How to set it up")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Recent messages" })).not.toBeInTheDocument();
  });

  it("a failed message-log read keeps the worker status and says what failed", async () => {
    renderRoute("/settings/integrations/zulip", { hub: integrationsHub({ "GET /api/zulip": { body: ZULIP_INBOUND_FAILED } }) });
    expect(await screen.findByText("Recent messages could not be read")).toBeInTheDocument();
    expect(screen.getByTestId("zulip-status")).toHaveTextContent("Failing");
    expect(screen.getByRole("table", { name: "Zulip channels the hub reads" })).toBeInTheDocument();
  });

  it("empty channels and messages say so", async () => {
    renderRoute("/settings/integrations/zulip", { hub: integrationsHub({ "GET /api/zulip": { body: ZULIP_EMPTY } }) });
    expect(await screen.findByText("No agent is set to read any Zulip channel.")).toBeInTheDocument();
    expect(screen.getByText("No Zulip messages have been received.")).toBeInTheDocument();
  });

  it("a failed read is an error with Retry", async () => {
    renderRoute("/settings/integrations/zulip", { hub: integrationsHub({ "GET /api/zulip": { status: 500, body: { error: "boom" } } }) });
    expect(await screen.findByRole("heading", { name: "Could not load the Zulip status" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("integrations: Notifications", () => {
  it("shows the last send, failures in a row with what to do, and the reason collapsed", async () => {
    renderRoute("/settings/integrations/notifications", { hub: integrationsHub() });
    await findPageHeading("Notifications");
    expect(await screen.findByTestId("notify-last")).toHaveTextContent("Last one not delivered");
    expect(screen.getByText("The last 2 notifications did not arrive")).toBeInTheDocument();
    expect(screen.getByTestId("notify-delivered")).toHaveTextContent("Delivered2");
    expect(screen.getByTestId("notify-failed")).toHaveTextContent("Not delivered2");
    const reasons = screen.getAllByText("Reason recorded");
    expect(reasons).toHaveLength(2);
    expect(screen.getByText("send.ts timed out after 30s")).not.toBeVisible();
  });

  it("a full page of results says the counts are of the last 20", async () => {
    renderRoute("/settings/integrations/notifications", { hub: integrationsHub({ "GET /api/notify_delivery": { body: NOTIFY_FULL } }) });
    expect(await screen.findByTestId("notify-delivered")).toHaveTextContent("Delivered, of the last 20");
  });

  it("no sends yet is an empty state", async () => {
    renderRoute("/settings/integrations/notifications", { hub: integrationsHub({ "GET /api/notify_delivery": { body: NOTIFY_EMPTY } }) });
    expect(await screen.findByRole("heading", { name: "No notifications sent yet" })).toBeInTheDocument();
  });

  it("an unavailable status is an error, never zero counts", async () => {
    renderRoute("/settings/integrations/notifications", { hub: integrationsHub({ "GET /api/notify_delivery": { body: NOTIFY_UNAVAILABLE } }) });
    expect(await screen.findByRole("heading", { name: "Could not load notification delivery" })).toBeInTheDocument();
    expect(screen.queryByTestId("notify-delivered")).not.toBeInTheDocument();
  });
});
