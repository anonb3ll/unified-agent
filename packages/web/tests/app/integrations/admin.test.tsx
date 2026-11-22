/**
 * The connector register, authorize and revoke forms (spec §8, §9.8) over
 * the hub routes `POST /api/action/connector/register | register-package |
 * authorize | revoke` (`webui/cc_agents.py`). Answers follow the hub: the
 * tool's result on 200, the `pending()` body on 202, a coded error body
 * otherwise; revoke of a revoked server answers `{already_revoked: true}`.
 */
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { draftKey, readDraft } from "../../../src/app/lib/drafts";
import { clearParkedWrites, listParkedWrites, runParkedWrite } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import type { MockReply } from "../support/fetchMock";
import { CONNECTORS, SERVER_GITHUB, SERVER_LINEAR_REVOKED } from "./fixtures";
import { integrationsHub, LOST, posts } from "./hub";

/** Attempt markers, in any owner's scope of session storage. */
const markers = () => Object.keys(sessionStorage).filter((k) => k.includes(":integrations-attempt:"));

/** The hub's 202 for a parked connector write (`cc_knowledge.pending`). */
const PARKED = (tool: string, approvalId: number): MockReply => ({
  status: 202,
  body: { status: "approval_pending", approval_id: approvalId, code: "approval_required", tool },
});

/** The tool's 200 result (`{"server": {...}, "approval_id", "event_id"}`). */
const REGISTERED = (serverId: string): MockReply => ({
  body: { server: { ...SERVER_GITHUB, server_id: serverId, status: "registered" }, approval_id: 81, event_id: 9001 },
});

/** A reply the test releases by hand. */
function held() {
  let release: (reply: MockReply) => void = () => {};
  const promise = new Promise<MockReply>((resolve) => (release = resolve));
  return { handler: () => promise, release: (reply: MockReply) => release(reply) };
}

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearParkedWrites(fixtureEpoch());
  sessionStorage.clear();
});

const PAGE = "/settings/integrations";
/** A stored write runs only under a whoami that names an organization. */
const ORG_WHOAMI: MockReply = { body: { ...whoamiFixture(), org: "org-1" } };
const REGISTER_DRAFT = draftKey({ identity: "operator", operation: "connector.register" });

async function openRegister(hub = integrationsHub()) {
  const rendered = renderRoute(PAGE, { hub });
  await findPageHeading("Integrations");
  await screen.findByTestId("package-status-github");
  fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
  return { ...rendered, dialog: await screen.findByRole("dialog", { name: "Register a connector" }) };
}

function fillPackage(dialog: HTMLElement, pkg = "int-gcp-agent20") {
  fireEvent.change(within(dialog).getByLabelText(/^Package/), { target: { value: pkg } });
  fireEvent.change(within(dialog).getByLabelText(/^Where it comes from/), { target: { value: "packaged" } });
  fireEvent.change(within(dialog).getByLabelText(/^Review reference/), { target: { value: "task-9" } });
}

function fillAuthorize(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText(/^Scope/), { target: { value: "repo:read" } });
  fireEvent.change(within(dialog).getByLabelText(/^Task/), { target: { value: "2478" } });
  fireEvent.change(within(dialog).getByLabelText(/^Trace id/), { target: { value: "t-1" } });
}

describe("integrations: connector administration forms", () => {
  it("register moves focus to the first invalid field and sends nothing", async () => {
    const { dialog, hub } = await openRegister();
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^Package/)).toHaveFocus());
    expect(within(dialog).getByText("Choose the package to register.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Package/)).toHaveAttribute("aria-invalid", "true");
    expect(hub.calls.some((c) => c.path.startsWith("/api/action/connector"))).toBe(false);
  });

  it("a package goes to register-package and a server to register, each with exactly the route's fields", async () => {
    const { dialog, hub } = await openRegister(
      integrationsHub({
        "POST /api/action/connector/register-package": REGISTERED("int-gcp-agent20"),
        "POST /api/action/connector/register": REGISTERED("gh-two"),
      }),
    );
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(posts(hub.calls, "/api/action/connector/register-package").map((c) => c.body)).toEqual([
      { package: "int-gcp-agent20", provenance: "packaged", review_ref: "task-9" },
    ]);
    expect(await screen.findByText("The connector is registered.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
    const again = await screen.findByRole("dialog", { name: "Register a connector" });
    fireEvent.click(within(again).getByRole("radio", { name: "A connector server" }));
    fireEvent.change(within(again).getByLabelText(/^Server id/), { target: { value: "gh-two" } });
    fireEvent.change(within(again).getByLabelText(/^Connector/), { target: { value: "github" } });
    fireEvent.change(within(again).getByLabelText(/^Where it comes from/), { target: { value: "packaged:int-github@1.4.0" } });
    fireEvent.change(within(again).getByLabelText(/^Scopes/), { target: { value: "repo:read, issues:write" } });
    fireEvent.change(within(again).getByLabelText(/^Review reference/), { target: { value: "task-1324" } });
    fireEvent.click(within(again).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/connector/register")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/connector/register")[0].body).toEqual({
      server_id: "gh-two",
      connector: "github",
      provenance: "packaged:int-github@1.4.0",
      scopes: ["repo:read", "issues:write"],
      review_ref: "task-1324",
    });
  });

  it("a 202 parks: never shown as done, the write is stored to run once approved, and the draft is kept", async () => {
    const { dialog, hub, epoch } = await openRegister(
      integrationsHub({ "POST /api/action/connector/register-package": PARKED("horizon2_server_register", 81) }),
    );
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    const notice = await within(dialog).findByRole("region", { name: "Approval status" });
    expect(notice).toBeInTheDocument();
    expect(screen.queryByText("The connector is registered.")).not.toBeInTheDocument();
    expect(markers()).toHaveLength(0);
    const stored = listParkedWrites(epoch());
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({
      path: "/api/action/connector/register-package",
      method: "POST",
      approvalId: "81",
      body: posts(hub.calls, "/api/action/connector/register-package")[0].body,
    });
    expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-9");
    expect(within(dialog).getByLabelText(/^Review reference/)).toHaveValue("task-9");
  });

  it("the parked write run once approved clears the submitted draft, read back from storage", async () => {
    let runs = 0;
    const { dialog, epoch, hub } = await openRegister(
      integrationsHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/approvals/81": { body: { id: 81, tool: "horizon2_server_register", status: "approved", requested_by: "operator" } },
        "POST /api/action/connector/register-package": () => (++runs === 1 ? PARKED("horizon2_server_register", 81) : REGISTERED("int-gcp-agent20")),
      }),
    );
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    fireEvent.click(await within(dialog).findByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/connector/register-package")).toHaveLength(2));
    expect(posts(hub.calls, "/api/action/connector/register-package")[1].body).toEqual(posts(hub.calls, "/api/action/connector/register-package")[0].body);
    await waitFor(() => expect(readDraft(epoch(), REGISTER_DRAFT)).toBeNull());
    await waitFor(() => expect(within(dialog).getByLabelText(/^Review reference/)).toHaveValue(""));
  });

  describe("a parked write replayed from the Inbox while the form is closed", () => {
    function approvedHub() {
      let runs = 0;
      return integrationsHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/approvals/81": { body: { id: 81, tool: "horizon2_server_register", status: "approved", requested_by: "operator" } },
        "POST /api/action/connector/register-package": () => (++runs === 1 ? PARKED("horizon2_server_register", 81) : REGISTERED("int-gcp-agent20")),
      });
    }

    async function parkThenLeave(change?: string) {
      const rendered = await openRegister(approvedHub());
      fillPackage(rendered.dialog);
      fireEvent.click(within(rendered.dialog).getByRole("button", { name: "Register" }));
      await within(rendered.dialog).findByRole("region", { name: "Approval status" });
      if (change) {
        fireEvent.change(within(rendered.dialog).getByLabelText(/^Review reference/), { target: { value: change } });
        await waitFor(() => expect(readDraft<{ reviewRef: string }>(rendered.epoch(), REGISTER_DRAFT)?.reviewRef).toBe(change));
      }
      fireEvent.click(within(rendered.dialog).getByRole("button", { name: "Cancel" }));
      await act(async () => {
        await rendered.router.navigate("/inbox");
      });
      await findPageHeading(/Inbox/);
      // The Inbox runs the stored write; the integrations page is not mounted.
      await act(async () => {
        expect((await runParkedWrite(listParkedWrites(rendered.epoch())[0].id, { epoch: rendered.epoch() })).kind).toBe("done");
      });
      await act(async () => {
        await rendered.router.navigate("/settings/integrations");
      });
      await screen.findByTestId("package-status-github");
      return rendered;
    }

    it("clears the submitted draft, so it is not restored on return", async () => {
      const { epoch } = await parkThenLeave();
      expect(readDraft(epoch(), REGISTER_DRAFT)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
      const again = await screen.findByRole("dialog", { name: "Register a connector" });
      expect(within(again).getByLabelText(/^Review reference/)).toHaveValue("");
      expect(within(again).queryByText("Restored what you typed earlier.")).not.toBeInTheDocument();
    });

    it("the run itself clears the draft, while the integrations page is not mounted (#2525)", async () => {
      const { dialog, epoch, router } = await openRegister(approvedHub());
      fillPackage(dialog);
      fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
      await within(dialog).findByRole("region", { name: "Approval status" });
      // The stored write carries the draft it came from.
      expect(listParkedWrites(epoch())[0].draft).toMatchObject({ draftKey: REGISTER_DRAFT, submittedSnapshot: { reviewRef: "task-9" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await act(async () => {
        await router.navigate("/inbox");
      });
      await findPageHeading(/Inbox/);
      expect(readDraft(epoch(), REGISTER_DRAFT)).not.toBeNull();
      await act(async () => {
        expect((await runParkedWrite(listParkedWrites(epoch())[0].id, { epoch: epoch() })).kind).toBe("done");
      });
      // Still on the Inbox: nothing on the integrations page had to mount for it.
      expect(readDraft(epoch(), REGISTER_DRAFT)).toBeNull();
    });

    it("keeps text typed after the send", async () => {
      const { epoch } = await parkThenLeave("task-10");
      expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-10");
      fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
      const again = await screen.findByRole("dialog", { name: "Register a connector" });
      expect(within(again).getByLabelText(/^Review reference/)).toHaveValue("task-10");
    });
  });

  it("a parked write run after the form changed keeps what was typed since", async () => {
    let runs = 0;
    const { dialog, epoch } = await openRegister(
      integrationsHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/approvals/81": { body: { id: 81, tool: "horizon2_server_register", status: "approved", requested_by: "operator" } },
        "POST /api/action/connector/register-package": () => (++runs === 1 ? PARKED("horizon2_server_register", 81) : REGISTERED("int-gcp-agent20")),
      }),
    );
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    await within(dialog).findByRole("region", { name: "Approval status" });
    fireEvent.change(within(dialog).getByLabelText(/^Review reference/), { target: { value: "task-10" } });
    await waitFor(() => expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-10"));
    await act(async () => {
      expect((await runParkedWrite(listParkedWrites(epoch())[0].id, { epoch: epoch() })).kind).toBe("done");
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-10");
    expect(within(dialog).getByLabelText(/^Review reference/)).toHaveValue("task-10");
  });

  it("coded refusals are explained in plain words, and the hub's own text is kept in the detail", async () => {
    const { dialog } = await openRegister(
      integrationsHub({
        "POST /api/action/connector/register-package": {
          status: 409,
          body: { error: "server int-gcp-agent20 is already registered", code: "already_registered" },
        },
      }),
    );
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    expect(await within(dialog).findByText(/^int-gcp-agent20 is already registered, so nothing was changed/)).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "This was not done" })).toBeInTheDocument();
    expect(markers()).toHaveLength(0);
    expect(within(dialog).getByRole("button", { name: "Register" })).toBeEnabled();
  });

  it("authorize of a server the hub no longer has says so (404 not_found)", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({
        "POST /api/action/connector/authorize": { status: 404, body: { error: "unregistered horizon 2 server: github-main", code: "not_found" } },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Authorize github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    fillAuthorize(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Authorize" }));
    expect(await within(dialog).findByText(/^github-main is not registered on this hub/)).toBeInTheDocument();
  });

  it("a hub without the routes answers 404: explained, not shown as success, and the draft is kept", async () => {
    let { dialog } = await openRegister(integrationsHub({ "POST /api/action/connector/register-package": { status: 404, body: { error: "Not Found" } } }));
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
    dialog = await screen.findByRole("dialog", { name: "Register a connector" });
    expect(within(dialog).getByLabelText(/^Review reference/)).toHaveValue("task-9");
    expect(within(dialog).getByText("Restored what you typed earlier.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    expect(await within(dialog).findByText(/This hub does not offer this action yet/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Review reference/)).toHaveValue("task-9");
  });

  it("authorize validates the task number and focuses it", async () => {
    renderRoute(PAGE, { hub: integrationsHub() });
    fireEvent.click(await screen.findByRole("button", { name: "Authorize github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    fireEvent.change(within(dialog).getByLabelText(/^Scope/), { target: { value: "repo:read" } });
    fireEvent.change(within(dialog).getByLabelText(/^Task/), { target: { value: "abc" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Authorize" }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^Task/)).toHaveFocus());
    expect(within(dialog).getByText("Enter a task number, such as 2478.")).toBeInTheDocument();
  });

  it("revoke confirms with the full consequence, and a lost answer blocks a blind retry", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "POST /api/action/connector/revoke": LOST }) });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    expect(within(dialog).getByText("github-main (github)")).toBeInTheDocument();
    expect(within(dialog).getByText(/every authorization for it stops working, and the revocation is permanent/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    expect(await within(dialog).findByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Revoke github-main" })).toBeDisabled();
  });

  it("revoke of a server revoked meanwhile says it already was", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({ "POST /api/action/connector/revoke": { body: { server: { ...SERVER_GITHUB, status: "revoked" }, already_revoked: true } } }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    expect(await screen.findByText("github-main was already revoked; nothing changed.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("records the attempt before the send; leaving mid-request and coming back shows the outcome as unknown until settled", async () => {
    const hold = held();
    let markerAtSend: string[] = [];
    const first = renderRoute(PAGE, {
      hub: integrationsHub({
        "POST /api/action/connector/revoke": () => {
          markerAtSend = markers();
          return hold.handler();
        },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    await waitFor(() => expect(posts(first.hub.calls, "/api/action/connector/revoke")).toHaveLength(1));
    expect(markerAtSend).toHaveLength(1);
    const stored = sessionStorage.getItem(markerAtSend[0]) ?? "";
    expect(JSON.parse(stored).body).toEqual({ server_id: "github-main" });
    // The person leaves while the request is in flight.
    first.unmount();
    cleanup();
    vi.unstubAllGlobals();

    const { hub } = renderRoute(PAGE, { hub: integrationsHub() });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const again = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    expect(within(again).getByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
    expect(within(again).getByRole("button", { name: "Revoke github-main" })).toBeDisabled();
    fireEvent.click(within(again).getByRole("button", { name: "It is not there, send it again" }));
    expect(within(again).getByRole("button", { name: "Revoke github-main" })).toBeEnabled();
    expect(markers()).toHaveLength(0);
    expect(posts(hub.calls, "/api/action/connector/revoke")).toHaveLength(0);
    hold.release({ status: 404, body: { error: "Not Found" } });
  });

  it("an answered request settles its marker: a refusal can be retried", async () => {
    renderRoute(PAGE, { hub: integrationsHub({ "POST /api/action/connector/revoke": { status: 403, body: { error: "denied", code: "policy_denied" } } }) });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    expect(await within(dialog).findByText(/A policy rule on the hub refused this/)).toBeInTheDocument();
    expect(markers()).toHaveLength(0);
    expect(within(dialog).getByRole("button", { name: "Revoke github-main" })).toBeEnabled();
  });

  it("success clears only the submitted snapshot: text typed while it was in flight stays in the draft", async () => {
    const hold = held();
    renderRoute(PAGE, { hub: integrationsHub({ "POST /api/action/connector/authorize": hold.handler }) });
    fireEvent.click(await screen.findByRole("button", { name: "Authorize github-main" }));
    let dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    fillAuthorize(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Authorize" }));
    await waitFor(() => expect(markers()).toHaveLength(1));
    // Typed after the request went out.
    fireEvent.change(within(dialog).getByLabelText(/^Connection reference/), { target: { value: "conn-2" } });
    await new Promise((r) => setTimeout(r, 400));
    hold.release({ body: { server: SERVER_GITHUB, approval_id: 82 } });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(markers()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Authorize github-main" }));
    dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    expect(within(dialog).getByLabelText(/^Connection reference/)).toHaveValue("conn-2");
  });

  it("a success that arrives after the form was closed and reopened never deletes newer text, nor closes the new form", async () => {
    const hold = held();
    const { dialog, epoch } = await openRegister(integrationsHub({ "POST /api/action/connector/register-package": hold.handler }));
    fillPackage(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(markers()).toHaveLength(1));
    // Closed while the request is in flight, then opened again on the same draft.
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Register a connector" }));
    const reopened = await screen.findByRole("dialog", { name: "Register a connector" });
    expect(within(reopened).getByLabelText(/^Review reference/)).toHaveValue("task-9");
    fireEvent.change(within(reopened).getByLabelText(/^Review reference/), { target: { value: "task-10" } });
    await waitFor(() => expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-10"));
    // The first send's success arrives now.
    hold.release(REGISTERED("int-gcp-agent20"));
    expect(await screen.findByText("The connector is registered.")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(readDraft<{ reviewRef: string }>(epoch(), REGISTER_DRAFT)?.reviewRef).toBe("task-10");
    expect(screen.getByRole("dialog", { name: "Register a connector" })).toBe(reopened);
    expect(within(reopened).getByLabelText(/^Review reference/)).toHaveValue("task-10");
  });

  it("success with nothing typed since clears the stored draft", async () => {
    const { epoch } = renderRoute(PAGE, { hub: integrationsHub({ "POST /api/action/connector/authorize": { body: { server: SERVER_GITHUB, approval_id: 82 } } }) });
    fireEvent.click(await screen.findByRole("button", { name: "Authorize github-main" }));
    let dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    fillAuthorize(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Authorize" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const key = draftKey({ identity: "operator", operation: "connector.authorize", object: { server: "github-main" } });
    expect(readDraft(epoch(), key)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Authorize github-main" }));
    dialog = await screen.findByRole("dialog", { name: "Authorize github-main" });
    expect(within(dialog).getByLabelText(/^Scope/)).toHaveValue("");
  });

  it("a result belongs to the object it was made for: another server's form shows none of it", async () => {
    renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/connectors": { body: { ...CONNECTORS, servers: [SERVER_GITHUB, { ...SERVER_GITHUB, server_id: "github-two" }] } },
        "POST /api/action/connector/revoke": { status: 403, body: { error: "denied", code: "policy_denied" } },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    let dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    await within(dialog).findByText(/A policy rule on the hub refused this/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Revoke github-two" }));
    dialog = await screen.findByRole("dialog", { name: "Revoke github-two?" });
    expect(within(dialog).queryByText(/A policy rule on the hub refused this/)).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Revoke github-two" })).toBeEnabled();
  });

  it("an unknown registration belongs to its target: another server id shows none of it, and keeps the first one's protection", async () => {
    const { dialog, hub } = await openRegister(integrationsHub({ "POST /api/action/connector/register": LOST }));
    fireEvent.click(within(dialog).getByRole("radio", { name: "A connector server" }));
    const serverId = () => within(dialog).getByLabelText(/^Server id/);
    fireEvent.change(serverId(), { target: { value: "gh-a" } });
    fireEvent.change(within(dialog).getByLabelText(/^Connector/), { target: { value: "github" } });
    fireEvent.change(within(dialog).getByLabelText(/^Where it comes from/), { target: { value: "p" } });
    fireEvent.change(within(dialog).getByLabelText(/^Scopes/), { target: { value: "repo:read" } });
    fireEvent.change(within(dialog).getByLabelText(/^Review reference/), { target: { value: "task-1" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    expect(await within(dialog).findByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Register" })).toBeDisabled();
    // Another target: no recovery state of gh-a's, and it can be sent.
    fireEvent.change(serverId(), { target: { value: "gh-b" } });
    await waitFor(() => expect(within(dialog).queryByRole("heading", { name: "It may have been done" })).not.toBeInTheDocument());
    expect(within(dialog).queryByRole("button", { name: "It is not there, send it again" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Register" })).toBeEnabled();
    // Back to gh-a: its protection is still there.
    fireEvent.change(serverId(), { target: { value: "gh-a" } });
    expect(await within(dialog).findByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Register" })).toBeDisabled();
    expect(markers()).toHaveLength(1);
    expect(posts(hub.calls, "/api/action/connector/register")).toHaveLength(1);
  });

  it("an unsettled attempt belongs to the identity that sent it", async () => {
    let agent = "operator";
    const { queryClient } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/whoami": () => ({ body: whoamiFixture({ agent }) }),
        "POST /api/action/connector/revoke": LOST,
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    let dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    expect(await within(dialog).findByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    agent = "Desktop_Agent2";
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: WHOAMI_QUERY_KEY });
    });
    await waitFor(() => expect(queryClient.getQueryData<{ agent: string }>(WHOAMI_QUERY_KEY)?.agent).toBe("Desktop_Agent2"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    expect(within(dialog).queryByRole("heading", { name: "It may have been done" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Revoke github-main" })).toBeEnabled();
  });

  it("an owner switch mid-write: nothing reaches the next owner, and the sender's marker survives as unknown", async () => {
    const hold = held();
    let agent = "operator";
    const { queryClient, epoch } = renderRoute(PAGE, {
      hub: integrationsHub({
        "GET /api/whoami": () => ({ body: whoamiFixture({ agent }) }),
        "POST /api/action/connector/revoke": hold.handler,
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke github-main" }));
    await waitFor(() => expect(markers()).toHaveLength(1));
    const senderMarker = markers()[0];
    expect(senderMarker).toContain('"operator"]');
    // Someone else becomes the verified owner while the request is in flight.
    agent = "Desktop_Agent2";
    await act(async () => {
      queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent }));
    });
    await waitFor(() => expect(epoch().owner?.identity).toBe("Desktop_Agent2"));
    hold.release({ body: { server: { ...SERVER_GITHUB, status: "revoked" }, approval_id: 83 } });
    await new Promise((r) => setTimeout(r, 100));
    // The new owner's page: a fresh tree, no dialog, no toast, no marker of theirs.
    await screen.findByRole("button", { name: "Revoke github-main" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("github-main is revoked.")).not.toBeInTheDocument();
    expect(markers()).toEqual([senderMarker]);
    // The sender comes back: the answer never reached their page, so it is unknown.
    agent = "operator";
    await act(async () => {
      queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent }));
    });
    await waitFor(() => expect(epoch().owner?.identity).toBe("operator"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const back = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    expect(within(back).getByRole("heading", { name: "It may have been done" })).toBeInTheDocument();
  });

  it("a refetch never moves the target of an open revoke dialog", async () => {
    let servers = [SERVER_GITHUB, SERVER_LINEAR_REVOKED];
    const { appClient } = renderRoute(PAGE, {
      hub: integrationsHub({ "GET /api/connectors": () => ({ body: { ...CONNECTORS, servers } }) }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke github-main" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke github-main?" });
    servers = [{ ...SERVER_GITHUB, server_id: "github-other", connector: "github" }];
    await act(async () => {
      await appClient().refetchQueries({ queryKey: ["integrations", "connectors"] });
    });
    await screen.findByTestId("server-status-github-other");
    expect(within(dialog).getByText("github-main (github)")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Revoke github-main" })).toBeInTheDocument();
  });
});
