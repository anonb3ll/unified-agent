import { afterEach, describe, expect, it, vi } from "vitest";
import {
  authorizeBody,
  checkAuthorizeUrl,
  describeIntegrationReadError,
  EMPTY_AUTHORIZE,
  EMPTY_REGISTER,
  failingStreak,
  fetchConnectorInventory,
  fetchNotifyStatus,
  fetchSlackStatus,
  fetchZulipStatus,
  IntegrationReadError,
  notifyCounts,
  parseScopes,
  registerBody,
  registerPath,
  connectorErrorText,
  revokeBody,
  safeExternalUrl,
  slackReasonAdvice,
  validateAuthorize,
  validateRegister,
} from "../../../src/app/integrations/api";
import { mockFetch } from "../support/fetchMock";
import {
  CONNECTORS,
  CONNECTORS_UNAVAILABLE,
  NOTIFY,
  NOTIFY_FULL,
  NOTIFY_UNAVAILABLE,
  SLACK_MISCONFIGURED,
  SLACK_UNAVAILABLE,
  ZULIP,
  ZULIP_INBOUND_FAILED,
  ZULIP_UNCONFIGURED,
} from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

/** A URL with a (fake) user name and password in it, built so no literal credential URL sits in the source. */
function withLogin(base: string): string {
  const url = new URL(base);
  url.username = "someone";
  url.password = "not-a-secret";
  return url.href;
}

describe("integrations api: reads", () => {
  it("a 200 carrying the hub's named error is a failed read, never an empty catalog", async () => {
    mockFetch({ "GET /api/connectors": { body: CONNECTORS_UNAVAILABLE } });
    const error = await fetchConnectorInventory().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(IntegrationReadError);
    expect((error as IntegrationReadError).status).toBe(200);
    expect(describeIntegrationReadError(error, "the connector registry").message).toBe(
      "The hub answered, but could not read the connector registry. Try again in a moment.",
    );
  });

  it("reads the connector inventory with its doctor checks", async () => {
    mockFetch({ "GET /api/connectors": { body: CONNECTORS } });
    const data = await fetchConnectorInventory();
    expect(data.servers.map((s) => s.server_id)).toEqual(["github-main", "linear-old"]);
    expect(data.packages).toHaveLength(2);
    expect(data.checks).toHaveLength(1);
  });

  it("slack and notify named errors are failed reads", async () => {
    mockFetch({ "GET /api/slack_gateway": { body: SLACK_UNAVAILABLE }, "GET /api/notify_delivery": { body: NOTIFY_UNAVAILABLE } });
    await expect(fetchSlackStatus()).rejects.toBeInstanceOf(IntegrationReadError);
    await expect(fetchNotifyStatus()).rejects.toBeInstanceOf(IntegrationReadError);
  });

  it("a non-JSON 200 is a failed read", async () => {
    mockFetch({ "GET /api/slack_gateway": { text: "<html>proxy</html>" } });
    await expect(fetchSlackStatus()).rejects.toBeInstanceOf(IntegrationReadError);
  });

  it("an HTTP failure keeps its status and explains it in plain words", async () => {
    mockFetch({ "GET /api/zulip": { status: 500, body: { error: "Traceback: sqlite3.OperationalError" } } });
    const error = await fetchZulipStatus().catch((e: unknown) => e);
    const described = describeIntegrationReadError(error, "the Zulip status");
    expect(described.message).toBe("The hub had a problem answering. Try again in a moment.");
    expect(described.message).not.toContain("sqlite3");
    expect(described.detail).toContain("sqlite3");
  });

  it("slack status maps presence only", async () => {
    mockFetch({ "GET /api/slack_gateway": { body: SLACK_MISCONFIGURED } });
    const data = await fetchSlackStatus();
    expect(data.status).toBe("MISCONFIGURED");
    expect(data.signingSecretPresent).toBe(false);
    expect(data.reasons).toHaveLength(2);
  });

  it("zulip: unconfigured is a successful read that says so; a failed inbound read keeps the worker", async () => {
    mockFetch({ "GET /api/zulip": { body: ZULIP_UNCONFIGURED } });
    const off = await fetchZulipStatus();
    expect(off.configurationError).toBe("UA_ZULIP_SITE is not set");
    vi.unstubAllGlobals();
    mockFetch({ "GET /api/zulip": { body: ZULIP_INBOUND_FAILED } });
    const partial = await fetchZulipStatus();
    expect(partial.configurationError).toBeNull();
    expect(partial.inbound).toBeNull();
    expect(partial.inboundError).toBe("database is locked");
    expect(partial.worker.channels).toHaveLength(2);
    vi.unstubAllGlobals();
    mockFetch({ "GET /api/zulip": { body: ZULIP } });
    const ok = await fetchZulipStatus();
    expect(ok.inbound).toHaveLength(2);
    expect(ok.stats.inbound24h).toBe(2);
  });
});

describe("integrations api: rules", () => {
  it("navigates only to https on the provider's own host, or to the hub's origin", () => {
    const hub = "https://hub.example.test";
    expect(checkAuthorizeUrl("https://github.com/login/oauth/authorize?state=x", "github.com", hub)).toMatchObject({ ok: true, host: "github.com" });
    expect(checkAuthorizeUrl("https://hub.example.test/oauth/x", "github.com", hub)).toMatchObject({ ok: true });
    expect(checkAuthorizeUrl("http://github.com/login", "github.com", hub)).toMatchObject({ ok: false });
    expect(checkAuthorizeUrl("javascript:alert(1)", "github.com", hub)).toMatchObject({ ok: false });
    expect(checkAuthorizeUrl("https://evil.example/github.com", "github.com", hub)).toMatchObject({
      ok: false,
      reason: "The sign-in address points to evil.example, not github.com as expected.",
    });
    expect(checkAuthorizeUrl("https://github.com.evil.example/", "github.com", hub)).toMatchObject({ ok: false });
    expect(checkAuthorizeUrl(withLogin("https://github.com/"), "github.com", hub)).toMatchObject({ ok: false });
    expect(checkAuthorizeUrl("not a url", "github.com", hub)).toMatchObject({ ok: false });
    expect(checkAuthorizeUrl(undefined, "github.com", hub)).toMatchObject({ ok: false });
  });

  it("external links render only for https without credentials", () => {
    expect(safeExternalUrl("https://zulip.example.test/#narrow")).toBe("https://zulip.example.test/#narrow");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("http://zulip.example.test")).toBeNull();
    expect(safeExternalUrl(withLogin("https://zulip.example.test/"))).toBeNull();
  });

  it("notify counts come from the rows read and say when capped", () => {
    expect(notifyCounts({ deliveries: NOTIFY.deliveries, last: null })).toEqual({ delivered: 2, failed: 2, total: 4, capped: false });
    expect(notifyCounts({ deliveries: NOTIFY_FULL.deliveries, last: null }).capped).toBe(true);
    expect(failingStreak(NOTIFY.deliveries)).toBe(2);
  });

  it("every slack reason the hub can give has advice", () => {
    for (const reason of [...SLACK_MISCONFIGURED.reasons, "UA_SLACK_ENABLED is not set", "UA_SLACK_USERS allowlist is empty", "UA_SLACK_CHANNEL_IDS / UA_SLACK_CHANNEL_MAP is empty"]) {
      expect(slackReasonAdvice(reason)).not.toBe(reason);
    }
    expect(slackReasonAdvice("something new")).toBe("something new");
  });

  it("register builds the server and package route bodies, exactly the route's fields (spec §8, task #2470)", () => {
    expect(parseScopes("repo:read, issues:write repo:read")).toEqual(["repo:read", "issues:write"]);
    expect(
      registerBody({ ...EMPTY_REGISTER, kind: "server", serverId: " gh ", connector: "github", provenance: "p", scopes: "a,b", reviewRef: "task-1" }),
    ).toEqual({ server_id: "gh", connector: "github", provenance: "p", scopes: ["a", "b"], review_ref: "task-1" });
    expect(registerPath({ ...EMPTY_REGISTER, kind: "server" })).toBe("/api/action/connector/register");
    expect(registerPath(EMPTY_REGISTER)).toBe("/api/action/connector/register-package");
    expect(registerBody({ ...EMPTY_REGISTER, packageName: "github", provenance: "p", reviewRef: "r" })).toEqual({
      package: "github",
      provenance: "p",
      review_ref: "r",
    });
    expect(Object.keys(validateRegister(EMPTY_REGISTER))).toEqual(["packageName", "provenance", "reviewRef"]);
    expect(Object.keys(validateRegister({ ...EMPTY_REGISTER, kind: "server", serverId: "bad id!" }))).toEqual([
      "serverId",
      "connector",
      "scopes",
      "provenance",
      "reviewRef",
    ]);
  });

  it("every code a connector route answers with is explained in plain words, never the raw text alone", () => {
    expect(connectorErrorText("already_registered", 409, "gh", "server gh is already registered")).toMatch(/^gh is already registered, so nothing was changed/);
    expect(connectorErrorText("not_found", 404, "gh", "unregistered horizon 2 server: gh")).toMatch(/^gh is not registered on this hub/);
    expect(connectorErrorText("operator_required", 403, "gh", "x")).toMatch(/human operator/);
    expect(connectorErrorText("invalid_request", 400, "gh", "missing scopes")).toBe("The hub refused the details, so nothing was changed: missing scopes");
    // A hub without the routes answers a bare 404.
    expect(connectorErrorText(undefined, 404, "gh", "Not Found")).toMatch(/does not offer this action yet/);
  });

  it("authorize and revoke build the tool parameters", () => {
    expect(validateAuthorize({ ...EMPTY_AUTHORIZE, serverId: "gh", taskId: "0" })).toMatchObject({ taskId: "Enter a task number, such as 2478." });
    expect(authorizeBody({ serverId: "gh", scope: "repo:read", taskId: "2478", traceId: "t-1", connectionRef: "" })).toEqual({
      server_id: "gh",
      scope: "repo:read",
      task_id: 2478,
      trace_id: "t-1",
    });
    expect(revokeBody("gh")).toEqual({ server_id: "gh" });
  });
});
