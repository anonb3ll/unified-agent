/**
 * Hosts page actions ported from the classic Devices view: machine details,
 * restarting the executor on the hub's machine, signed-in devices and their
 * revoke, approver passkeys (register with WebAuthn, remove, the loopback
 * and wrong-device explanations) and attesting a device's acceptance.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { canAttest } from "../../../src/app/agents/HostsPage";
import { b64urlToBuffer, bufferToB64url, issuerHostsLink } from "../../../src/app/agents/passkeys";
import { clearParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { findPageHeading, renderRoute } from "../support/render";
import { agentsHub, DEVICES, NODES, parked, PASSKEYS } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const open = async (overrides = {}) => {
  const result = renderRoute("/agents/hosts", { hub: agentsHub(overrides) });
  await findPageHeading("Hosts");
  return result;
};
const posts = (hub: { calls: { method: string; path: string; body: unknown }[] }, path: string) => hub.calls.filter((c) => c.method === "POST" && c.path === path).map((c) => c.body);

const PENDING_PASS = { state: "pending", run_id: "acc-0003", checks: { launch: "pass", progress: "pass", receipt: "pass" } };
const nodesWith = (acceptance: unknown) => ({ body: { ...NODES, nodes: [{ ...NODES.nodes[0], acceptance }] } });

describe("hosts: machines", () => {
  it("shows doctor, worktrees and freshness per machine, and agents with no machine with their credential", async () => {
    await open();
    const machines = await screen.findByRole("table", { name: "Machines" });
    expect(machines).toHaveTextContent("live · 3 working / 0 off / 1 degraded");
    expect(machines).toHaveTextContent("2 · 1 stale");
    const unplaced = screen.getByRole("list", { name: "Agents with no machine" });
    expect(unplaced).toHaveTextContent('cloud_agent17 idle · says it runs on "cloud-box", which is not a known machine · token #12');
  });

  it("restarts the executor only on the hub's machine, after a confirm", async () => {
    const { hub } = await open({ "POST /api/action/fleet": { body: { ok: true } } });
    const machines = await screen.findByRole("table", { name: "Machines" });
    expect(within(machines).getAllByRole("button", { name: /Restart executor/ })).toHaveLength(1);
    expect(machines).toHaveTextContent("Only on the hub's machine");
    fireEvent.click(within(machines).getByRole("button", { name: "Restart executor on desktop" }));
    const dialog = await screen.findByRole("dialog", { name: "Restart the executor on desktop?" });
    expect(posts(hub, "/api/action/fleet")).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Restart executor" }));
    await waitFor(() => expect(posts(hub, "/api/action/fleet")).toEqual([{ action: "restart_service", host: "desktop", unit: "unified-agent-executor.service" }]));
    expect(await within(machines).findByText("Restart sent.")).toBeInTheDocument();
  });

  it("shows a parked restart as waiting for approval, not as sent", async () => {
    await open({
      "POST /api/action/fleet": { status: 202, body: parked("fleet_restart", 41) },
      "GET /api/approvals/41": { body: { id: 41, tool: "fleet_restart", status: "pending", requested_by: "operator" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Restart executor on desktop" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Restart executor" }));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    expect(screen.queryByText("Restart sent.")).not.toBeInTheDocument();
  });
});

describe("hosts: signed-in devices", () => {
  it("lists sign-ins by device and revokes a device's sign-in or an unbound session after a confirm", async () => {
    const { hub } = await open({
      "POST /api/action/oauth-revoke": { status: 202, body: parked("oauth_revoke", 7) },
      "GET /api/approvals/7": { body: { id: 7, tool: "oauth_revoke", status: "pending", requested_by: "operator" } },
    });
    const table = await screen.findByRole("table", { name: "Signed-in devices" });
    expect(table).toHaveTextContent("laptop-node");
    expect(table).toHaveTextContent("Laptop_Agent2");
    expect(table).toHaveTextContent("No device binding");
    fireEvent.click(within(table).getByRole("button", { name: "Revoke sign-in for laptop-node" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke the sign-in for laptop-node?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(posts(hub, "/api/action/oauth-revoke")).toEqual([{ device: "n123" }]));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: /Revoke session bbbbbbbb/ }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(posts(hub, "/api/action/oauth-revoke")).toEqual([{ device: "n123" }, { session_id: "b".repeat(16) }]));
  });

  it("does not show sign-ins when the hub has OAuth sign-in off", async () => {
    await open({ "GET /api/devices": { body: { ...DEVICES, oauth: null } } });
    await screen.findByRole("table", { name: "Machines" });
    expect(screen.queryByRole("region", { name: "Signed-in devices" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approver passkeys" })).not.toBeInTheDocument();
  });
});

describe("hosts: approver passkeys", () => {
  const fakeCredential = () => {
    const bytes = (n: number) => new Uint8Array([n, n + 1, n + 2]).buffer;
    return {
      rawId: bytes(1),
      response: { clientDataJSON: bytes(4), getAuthenticatorData: () => bytes(7), getPublicKey: () => bytes(10), getPublicKeyAlgorithm: () => -7 },
    };
  };

  it("lists passkeys and lets this device's approver remove only their own, after a confirm", async () => {
    const { hub } = await open({ "POST /api/oauth/passkeys/delete": { body: { ok: true } } });
    const table = await screen.findByRole("table", { name: "Approver passkeys" });
    expect(table).toHaveTextContent("Laptop Touch ID");
    expect(table).toHaveTextContent("Other approver");
    expect(within(table).getAllByRole("button", { name: /^Remove/ })).toHaveLength(1);
    fireEvent.click(within(table).getByRole("button", { name: "Remove Laptop Touch ID" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove the passkey Laptop Touch ID?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove passkey" }));
    await waitFor(() => expect(posts(hub, "/api/oauth/passkeys/delete")).toEqual([{ credential_id: PASSKEYS.passkeys[0].credential_id }]));
  });

  it("registers a passkey: options from the hub, the authenticator, then the signed registration", async () => {
    const create = vi.fn(async () => fakeCredential());
    vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
    Object.defineProperty(navigator, "credentials", { configurable: true, value: { create } });
    const { hub } = await open({
      "POST /api/oauth/passkeys/options": {
        body: { challenge: "AAEC", rp: { id: "hub.example.test", name: "Hub" }, user: { id: "AQID", name: "approver", displayName: "Approver" }, pubKeyCredParams: [{ type: "public-key", alg: -7 }], excludeCredentials: [] },
      },
      "POST /api/oauth/passkeys": { status: 201, body: { ok: true } },
    });
    const form = await screen.findByRole("form", { name: "Register a passkey" });
    fireEvent.change(within(form).getByLabelText(/^Passkey label/), { target: { value: "Desk key" } });
    fireEvent.click(within(form).getByRole("button", { name: "Register a passkey for approver@example.com" }));
    await waitFor(() => expect(posts(hub, "/api/oauth/passkeys")).toHaveLength(1));
    expect(create).toHaveBeenCalledTimes(1);
    expect(posts(hub, "/api/oauth/passkeys/options")).toEqual([{}]);
    expect(posts(hub, "/api/oauth/passkeys")[0]).toEqual({
      credential_id: bufferToB64url(new Uint8Array([1, 2, 3]).buffer),
      client_data_json: bufferToB64url(new Uint8Array([4, 5, 6]).buffer),
      authenticator_data: bufferToB64url(new Uint8Array([7, 8, 9]).buffer),
      public_key: bufferToB64url(new Uint8Array([10, 11, 12]).buffer),
      public_key_algorithm: -7,
      label: "Desk key",
    });
    await waitFor(() => expect(within(form).getByLabelText(/^Passkey label/)).toHaveValue(""));
    Reflect.deleteProperty(navigator, "credentials");
  });

  it("says when the person dismisses the passkey prompt, and sends nothing to register", async () => {
    vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
    Object.defineProperty(navigator, "credentials", {
      configurable: true,
      value: { create: vi.fn(async () => Promise.reject(new DOMException("denied", "NotAllowedError"))) },
    });
    const { hub } = await open({
      "POST /api/oauth/passkeys/options": { body: { challenge: "AAEC", rp: { id: "h", name: "h" }, user: { id: "AQID", name: "a", displayName: "a" }, pubKeyCredParams: [] } },
    });
    const form = await screen.findByRole("form", { name: "Register a passkey" });
    fireEvent.click(within(form).getByRole("button", { name: /Register a passkey/ }));
    expect(await within(form).findByText("Passkey creation was cancelled or timed out.")).toBeInTheDocument();
    expect(posts(hub, "/api/oauth/passkeys")).toEqual([]);
    Reflect.deleteProperty(navigator, "credentials");
  });

  it("explains a loopback request and links to the Hosts page on the hub's issuer", async () => {
    await open({ "GET /api/oauth/passkeys": { body: { ...PASSKEYS, this_device_login: null, peer_is_loopback: true } } });
    expect(await screen.findByText(/hub sees this request from a loopback address/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://hub.example.test/ui/agents/hosts#approver-passkeys" })).toHaveAttribute("href", "https://hub.example.test/ui/agents/hosts#approver-passkeys");
    expect(screen.queryByRole("form", { name: "Register a passkey" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
  });

  it("keeps the page working when the issuer origin is malformed, and says when no approver is configured", async () => {
    expect(issuerHostsLink("https://hub.example.test:bad")).toBeNull();
    await open({ "GET /api/oauth/passkeys": { body: { ...PASSKEYS, origin: "https://hub.example.test:bad", this_device_login: null } } });
    expect(await screen.findByText(/Open the hub directly from a tailnet device owned by approver@example.com/)).toBeInTheDocument();
    cleanup();
    await open({ "GET /api/oauth/passkeys": { body: { passkeys: [], approver_logins: [] } } });
    expect(await screen.findByText(/No approver login is configured/)).toBeInTheDocument();
  });

  it("round-trips base64url", () => {
    expect(bufferToB64url(b64urlToBuffer("AQID-_8"))).toBe("AQID-_8");
  });
});

describe("hosts: attesting acceptance", () => {
  it("offers Attest only for a pending run whose checks all passed", () => {
    expect(canAttest(PENDING_PASS)).toBe(true);
    expect(canAttest({ ...PENDING_PASS, checks: { launch: "pass", receipt: "fail" } })).toBe(false);
    expect(canAttest({ ...PENDING_PASS, state: "accepted" })).toBe(false);
    expect(canAttest({ ...PENDING_PASS, run_id: null })).toBe(false);
    expect(canAttest({ ...PENDING_PASS, checks: {} })).toBe(false);
  });

  it("attests only with independent evidence and an explicit confirm", async () => {
    const { hub } = await open({ "GET /api/nodes": nodesWith(PENDING_PASS), "POST /api/action/node-acceptance": { body: { state: "accepted", run_id: "acc-0003" } } });
    const devices = await screen.findByRole("table", { name: "Registered devices" });
    expect(devices).toHaveTextContent("3/3 checks pass");
    fireEvent.click(within(devices).getByRole("button", { name: "Attest acceptance on desktop-node" }));
    const dialog = await screen.findByRole("dialog", { name: "Attest desktop-node's acceptance?" });
    const confirm = within(dialog).getByRole("button", { name: "Attest acceptance" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/^Independent evidence/), { target: { value: "   " } });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/^Independent evidence/), { target: { value: "  checked output and cancel  " } });
    fireEvent.click(confirm);
    await waitFor(() => expect(posts(hub, "/api/action/node-acceptance")).toHaveLength(1));
    expect(posts(hub, "/api/action/node-acceptance")[0]).toEqual({ action: "attest", node: "desktop-node", run_id: "acc-0003", evidence_ref: "checked output and cancel" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the dialog open with the refusal when the hub refuses an attestation by the device's own agent", async () => {
    await open({
      "GET /api/nodes": nodesWith(PENDING_PASS),
      "POST /api/action/node-acceptance": { status: 403, body: { error: "a node's own agent cannot attest its acceptance" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Attest acceptance on desktop-node" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Independent evidence/), { target: { value: "verified" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Attest acceptance" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/own agent cannot attest|may not do this/);
    expect(within(dialog).getByLabelText(/^Independent evidence/)).toHaveValue("verified");
  });

  it("a parked attestation is waiting, and reopening it keeps the exact evidence", async () => {
    const { hub } = await open({
      "GET /api/nodes": nodesWith(PENDING_PASS),
      "POST /api/action/node-acceptance": { status: 202, body: parked("node_acceptance", 15) },
      "GET /api/approvals/15": { body: { id: 15, tool: "node_acceptance", status: "pending", requested_by: "operator" } },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Attest acceptance on desktop-node" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Independent evidence/), { target: { value: "verified sha256 abc" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Attest acceptance" }));
    expect(await screen.findByRole("region", { name: "Approval status" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Attest acceptance on desktop-node" }));
    const again = await screen.findByRole("dialog");
    expect(within(again).getByLabelText(/^Independent evidence/)).toHaveValue("verified sha256 abc");
    expect(posts(hub, "/api/action/node-acceptance")).toHaveLength(1);
  });
});

describe("hosts: recovering a finished acceptance run", () => {
  const FAILED = { state: "failed", run_id: "acc-0004", reason: "cancel_confirmed failed", checks: { launch: "pass", cancel_confirmed: "fail" } };
  const TIMED_OUT = { state: "failed", run_id: "acc-0005", reason: "timed_out after 900s; start a fresh run", checks: { launch: "pass" } };

  it.each([
    ["a failed run", FAILED],
    ["a timed-out run", TIMED_OUT],
  ])("offers a fresh run after %s and starts one on confirm", async (_label, acceptance) => {
    const { hub } = await open({ "GET /api/nodes": nodesWith(acceptance), "POST /api/action/node-acceptance": { body: { state: "pending", run_id: "acc-0009" } } });
    const devices = await screen.findByRole("table", { name: "Registered devices" });
    fireEvent.click(within(devices).getByRole("button", { name: "Run acceptance again on desktop-node" }));
    const dialog = await screen.findByRole("dialog", { name: "Run acceptance on desktop-node?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run acceptance" }));
    await waitFor(() => expect(posts(hub, "/api/action/node-acceptance")).toHaveLength(1));
    expect(posts(hub, "/api/action/node-acceptance")[0]).toEqual({ action: "start", node: "desktop-node" });
  });

  it("offers only Re-check while a run is pending", async () => {
    await open({ "GET /api/nodes": nodesWith(PENDING_PASS) });
    const devices = await screen.findByRole("table", { name: "Registered devices" });
    expect(within(devices).getByRole("button", { name: "Re-check desktop-node" })).toBeInTheDocument();
    expect(within(devices).queryByRole("button", { name: /Run acceptance/ })).not.toBeInTheDocument();
  });
});
