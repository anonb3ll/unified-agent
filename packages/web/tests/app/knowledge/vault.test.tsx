import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bumpAuthGeneration } from "../../../src/app/lib/authGeneration";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import type { MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { DEPLOYS_CONTENT, DEPLOYS_LINKS, DEPLOYS_PATH, INGEST, LOCKED_PATH, vaultPage } from "./fixtures";
import { knowledgeHub, PARKED, posts } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  // Unmount first: a mounted form flushes its draft on unmount, which would
  // otherwise land in the next test's storage.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const noteUrl = (path: string, extra = "") => `/knowledge/vault?path=${encodeURIComponent(path)}${extra}`;

describe("knowledge: vault browsing", () => {
  it("shows today's journal and the Inbox captures with their Wiki status", async () => {
    renderRoute("/knowledge/vault", { hub: knowledgeHub() });
    await findPageHeading("Vault");
    expect(await screen.findByText("Opened PR #1510 for the kernel batch.")).toBeInTheDocument();
    const inbox = screen.getByRole("region", { name: "Inbox captures" });
    expect(within(inbox).getByRole("link", { name: "00 - Inbox/idea.md" })).toHaveAttribute("href", "/ui/knowledge/vault?path=00+-+Inbox%2Fidea.md");
    expect(within(inbox).getByText("Not yet in the Wiki")).toBeInTheDocument();
  });

  it("the folder tree opens folders on demand and links notes by query string", async () => {
    const { hub } = renderRoute("/knowledge/vault", { hub: knowledgeHub() });
    const tree = await screen.findByRole("navigation", { name: "Vault folders" }, { timeout: 12_000 });
    const wiki = await within(tree).findByRole("button", { name: /Wiki/ });
    expect(wiki).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(wiki);
    expect(wiki).toHaveAttribute("aria-expanded", "true");
    const link = await within(tree).findByRole("link", { name: "Deploys.v2.md" });
    expect(link).toHaveAttribute("href", `/ui/knowledge/vault?path=${encodeURIComponent(DEPLOYS_PATH).replace(/%20/g, "+")}`);
    expect(hub.calls.some((c) => c.path === "/api/vault/list")).toBe(true);
  });

  it("reads a note with Markdown, wikilinks that navigate in the app, backlinks and ingest status", async () => {
    const { router } = renderRoute(noteUrl(DEPLOYS_PATH), { hub: knowledgeHub() });
    const heading = await screen.findByRole("heading", { level: 2, name: "Deploys.v2" });
    const section = heading.closest("section") as HTMLElement;
    expect(within(section).getByRole("heading", { level: 3, name: "Deploys" })).toBeInTheDocument();
    expect(section.querySelector("b")).toBeNull();
    expect(within(section).getByText(/<b>not bold<\/b>/)).toBeInTheDocument();
    expect(await within(section).findByText("Changed since it went into the Wiki")).toBeInTheDocument();
    // The tree opened the note's folder and marks the note.
    const tree = screen.getByRole("navigation", { name: "Vault folders" });
    expect(await within(tree).findByRole("link", { name: "Deploys.v2.md" })).toHaveAttribute("aria-current", "page");
    const links = screen.getByRole("region", { name: "Links" });
    expect(await within(links).findByRole("link", { name: "Wiki/Index.md" })).toHaveAttribute("href", "/ui/knowledge/vault?path=Wiki%2FIndex.md");
    // A wikilink resolves beside the linking note and replaces the URL with the note's path.
    const wikilink = within(section).getByRole("link", { name: "Runbook" });
    expect(wikilink.getAttribute("href")).toContain("link=Runbook");
    fireEvent.click(wikilink);
    // Two reads (resolve, then the page) and a URL replace: allow for a loaded machine.
    expect(await screen.findByRole("heading", { level: 2, name: "Runbook" }, { timeout: 12_000 })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toBe("?path=Wiki%2FRunbook.md"));
  });

  it("ingest status and links refresh when the note's revision changes (review 1, finding 7)", async () => {
    let revision = "sha256:one";
    let ingestReads = 0;
    let linkReads = 0;
    const { appClient } = renderRoute(noteUrl(DEPLOYS_PATH), {
      hub: knowledgeHub({
        "GET /api/vault/page": () => ({ body: { ...vaultPage(DEPLOYS_PATH, revision === "sha256:one" ? DEPLOYS_CONTENT : `${DEPLOYS_CONTENT}\nNew line.\n`, DEPLOYS_LINKS), revision } }),
        "GET /api/vault/ingest-status": () => ({ body: { ...INGEST, status: ++ingestReads === 1 ? "unchanged" : "changed" } }),
        "GET /api/vault/links": () => {
          linkReads++;
          return { body: DEPLOYS_LINKS };
        },
      }),
    });
    expect(await screen.findByText("In the Wiki")).toBeInTheDocument();
    await waitFor(() => expect(linkReads).toBe(1));
    revision = "sha256:two";
    await act(async () => {
      await appClient().refetchQueries({ queryKey: ["knowledge", "vault", "page", DEPLOYS_PATH], exact: true });
    });
    expect(await screen.findByText("Changed since it went into the Wiki")).toBeInTheDocument();
    await waitFor(() => expect(linkReads).toBe(2));
  });

  it("a wikilink to no note offers a search", async () => {
    renderRoute("/knowledge/vault?link=Nowhere&from=Wiki%2FIndex.md", { hub: knowledgeHub() });
    expect(await screen.findByRole("heading", { name: "No note named “Nowhere”" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Search for “Nowhere”" })).toHaveAttribute("href", "/ui/knowledge/vault?q=Nowhere");
  });

  it("searches the vault from the URL", async () => {
    renderRoute("/knowledge/vault?q=steward", { hub: knowledgeHub() });
    const results = await screen.findByRole("region", { name: "Search results for “steward”" });
    expect(await within(results).findByText("2 matching lines")).toBeInTheDocument();
    expect(within(results).getByRole("link", { name: "Wiki/Index.md" })).toBeInTheDocument();
  });

  it("a hub without a vault says so in plain words", async () => {
    renderRoute("/knowledge/vault", {
      hub: knowledgeHub({
        "GET /api/vault/briefing": { body: { error: "vault not configured", code: "vault_not_configured", entries: [], inbox: [] } },
        "GET /api/vault/list": { status: 400, body: { error: "vault tools are disabled; set UA_VAULT_PATH to an explicit vault root" } },
      }),
    });
    const messages = await screen.findAllByText(/This hub has no vault connected/);
    expect(messages.length).toBeGreaterThanOrEqual(2);
  });

  it("a locked note says it is locked and offers no write", async () => {
    renderRoute(noteUrl(LOCKED_PATH), { hub: knowledgeHub() });
    expect(await screen.findByText(/This note is locked/)).toBeInTheDocument();
    expect(screen.getAllByText("Locked").some((el) => el.tagName === "SPAN")).toBe(true);
    expect(screen.queryByRole("link", { name: "Add to this note" })).not.toBeInTheDocument();
  });

  it("a note whose lock cannot be read is offered with a warning", async () => {
    renderRoute(noteUrl("Wiki/Odd.md"), {
      hub: knowledgeHub({ "GET /api/vault/page": { body: vaultPage("Wiki/Odd.md", "---\n{locked: true}\n---\n# Odd\n") } }),
    });
    expect(await screen.findByText(/may be locked/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add to this note" })).toBeInTheDocument();
  });

  it("a note in a write folder offers an addition; one outside is read only", async () => {
    const first = renderRoute(noteUrl("Logs/2026-11-10.md"), { hub: knowledgeHub() });
    await screen.findByRole("heading", { level: 2, name: "2026-11-10" });
    expect(screen.getByRole("link", { name: "Add to this note" })).toHaveAttribute("href", "/ui/knowledge/vault?path=Logs%2F2026-11-10.md&write=append");
    first.unmount();
    renderRoute(noteUrl("README.md"), { hub: knowledgeHub() });
    await screen.findByRole("heading", { level: 2, name: "README" });
    expect(screen.getByText("Read only here: the hub writes only to 00 - Inbox, Logs, Wiki.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add to this note" })).not.toBeInTheDocument();
  });
});

describe("knowledge: vault writes", () => {
  it("writes a new note only into a writable folder, create-only", async () => {
    const { hub, router } = renderRoute("/knowledge/vault?write=new", {
      hub: knowledgeHub({ "POST /api/action/vault/write": { body: { path: "Wiki/Plan.md", bytes_written: 9, mode: "overwrite", append: false, total_lines_after: 1, revision: "sha256:1" } } }),
    });
    const form = await screen.findByRole("region", { name: "New vault note" });
    expect(within(form).getByText("00 - Inbox, Logs, Wiki")).toBeInTheDocument();
    const folder = within(form).getByLabelText(/^Folder/);
    expect(within(folder).getAllByRole("option").map((o) => o.textContent)).toEqual(expect.arrayContaining(["00 - Inbox", "Logs", "Wiki"]));
    fireEvent.change(folder, { target: { value: "Wiki" } });
    fireEvent.change(within(form).getByLabelText(/^Note name/), { target: { value: "Plan" } });
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "# Plan" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/vault/write")[0]?.body).toEqual({ path: "Wiki/Plan.md", content: "# Plan", mode: "overwrite", expected_revision: "absent" }),
    );
    await waitFor(() => expect(router.state.location.search).toBe("?path=Wiki%2FPlan.md"));
  });

  it("learns the real write folders from the hub's refusal and names them", async () => {
    renderRoute("/knowledge/vault?write=new", {
      hub: knowledgeHub({
        "POST /api/action/vault/write": {
          status: 400,
          body: { error: "vault writes are restricted to 00 - Inbox, Logs; 'Wiki/Plan.md' is outside. Extend UA_VAULT_WRITE_DIRS (colon-separated) to widen it." },
        },
      }),
    });
    const form = await screen.findByRole("region", { name: "New vault note" });
    fireEvent.change(within(form).getByLabelText(/^Folder/), { target: { value: "Wiki" } });
    fireEvent.change(within(form).getByLabelText(/^Note name/), { target: { value: "Plan" } });
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("The hub writes only to these folders: 00 - Inbox, Logs. Pick one of them.");
    await waitFor(() => expect(within(within(form).getByLabelText(/^Folder/)).getAllByRole("option").map((o) => o.textContent)).not.toContain("Wiki"));
    // The draft survives the refusal.
    expect(within(form).getByLabelText(/^Text/)).toHaveValue("x");
  });

  it("appends to a note and explains a locked refusal", async () => {
    const { hub } = renderRoute(noteUrl(DEPLOYS_PATH, "&write=append"), {
      hub: knowledgeHub({
        "POST /api/action/vault/write": { status: 400, body: { error: `note '${DEPLOYS_PATH}' is locked (frontmatter \`locked: true\`); pass force=True only with human approval` } },
      }),
    });
    const form = await screen.findByRole("form", { name: "Add to this note" });
    fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "- new line" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/vault/write")[0]?.body).toEqual({ path: DEPLOYS_PATH, content: "\n- new line", mode: "append" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/This note is locked/);
  });

  describe("an unknown outcome is never guessed (review 1 #3, review 2 #6-8)", () => {
    const lost = () => Promise.reject(new TypeError("network connection was lost"));

    async function sendAppend(hubOverrides: Record<string, MockHandler>) {
      const rendered = renderRoute(noteUrl(DEPLOYS_PATH, "&write=append"), { hub: knowledgeHub(hubOverrides) });
      const form = await screen.findByRole("form", { name: "Add to this note" }, { timeout: 12_000 });
      await screen.findByRole("button", { name: "Account: operator" });
      fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "- added line" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
      return { ...rendered, form };
    }

    it("append: the note already holding the text proves nothing; the person decides", async () => {
      const { hub, form } = await sendAppend({
        "POST /api/action/vault/write": lost,
        // The note now contains the text and has a new revision: still not proof.
        "GET /api/vault/page": { body: { ...vaultPage(DEPLOYS_PATH, `${DEPLOYS_CONTENT}\n- added line`, DEPLOYS_LINKS), revision: "sha256:after" } },
      });
      const panel = await within(form).findByRole("region", { name: "Not known whether it was saved" });
      expect(within(panel).getByRole("link", { name: "Open the note and look for your text" })).toHaveAttribute("href", "/ui/knowledge/vault?path=Wiki%2FDeploys.v2.md");
      expect(within(form).getByRole("button", { name: "Add text" })).toBeDisabled();
      expect(within(form).getByLabelText(/^Text to add/)).toHaveValue("- added line");
      expect(within(form).queryByText(/Your text is in the note now/)).toBeNull();
      fireEvent.click(within(panel).getByRole("button", { name: "It is there" }));
      await waitFor(() => expect(within(form).getByLabelText(/^Text to add/)).toHaveValue(""));
      expect(posts(hub.calls, "/api/action/vault/write")).toHaveLength(1);
    });

    it("append: sending again needs a confirm that says a duplicate is possible", async () => {
      let n = 0;
      const { hub, form } = await sendAppend({
        "POST /api/action/vault/write": () => (++n === 1 ? lost() : { body: { path: DEPLOYS_PATH, bytes_written: 14, mode: "append", append: true, total_lines_after: 7, revision: "sha256:2" } }),
      });
      const panel = await within(form).findByRole("region", { name: "Not known whether it was saved" });
      fireEvent.click(within(panel).getByRole("button", { name: "It is not there, send it again" }));
      const dialog = await screen.findByRole("dialog", { name: "Send it again?" });
      expect(dialog).toHaveTextContent(/twice/);
      expect(posts(hub.calls, "/api/action/vault/write")).toHaveLength(1);
      fireEvent.click(within(dialog).getByRole("button", { name: "Send it again" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/vault/write")).toHaveLength(2));
    });

    it("append: leaving before the answer arrives leaves the attempt marked, so returning shows it unknown", async () => {
      const first = await sendAppend({ "POST /api/action/vault/write": () => new Promise(() => undefined) });
      await waitFor(() => expect(posts(first.hub.calls, "/api/action/vault/write")).toHaveLength(1));
      first.unmount();
      renderRoute(noteUrl(DEPLOYS_PATH, "&write=append"), { hub: knowledgeHub() });
      const form = await screen.findByRole("form", { name: "Add to this note" }, { timeout: 12_000 });
      expect(await within(form).findByRole("region", { name: "Not known whether it was saved" })).toBeInTheDocument();
      expect(within(form).getByRole("button", { name: "Add text" })).toBeDisabled();
    });

    it("append: a refused send is not unknown, and the form stays usable", async () => {
      const { form } = await sendAppend({ "POST /api/action/vault/write": { status: 400, body: { error: "content is longer than 200000 characters" } } });
      await within(form).findByRole("alert");
      expect(within(form).queryByRole("region", { name: "Not known whether it was saved" })).toBeNull();
      expect(within(form).getByRole("button", { name: "Add text" })).toBeEnabled();
    });

    it("append: a send cancelled after it left (sign-in changed) is unknown for the same person, not a failure", async () => {
      let release: (() => void) | null = null;
      await sendAppend({
        "POST /api/action/vault/write": () =>
          new Promise((resolve) => {
            release = () => resolve({ body: { path: DEPLOYS_PATH, bytes_written: 14, mode: "append", append: true, total_lines_after: 7, revision: "sha256:2" } });
          }),
      });
      await waitFor(() => expect(release).not.toBeNull());
      await act(async () => {
        bumpAuthGeneration();
        release?.();
      });
      // The new auth generation revokes the owner epoch: the page remounts under a new one.
      const form = await screen.findByRole("form", { name: "Add to this note" }, { timeout: 12_000 });
      expect(await within(form).findByRole("region", { name: "Not known whether it was saved" })).toBeInTheDocument();
      expect(within(form).getByLabelText(/^Text to add/)).toHaveValue("- added line");
    });

    it("append: \"It is there\" keeps text typed after the attempt (review 3, item 5)", async () => {
      let fail: (() => void) | null = null;
      const { form } = await sendAppend({
        "POST /api/action/vault/write": () =>
          new Promise((_, reject) => {
            fail = () => reject(new TypeError("network connection was lost"));
          }),
      });
      await waitFor(() => expect(fail).not.toBeNull());
      fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "- added line\n- and a second one" } });
      await act(async () => fail?.());
      const panel = await within(form).findByRole("region", { name: "Not known whether it was saved" });
      fireEvent.click(within(panel).getByRole("button", { name: "It is there" }));
      expect(within(form).getByLabelText(/^Text to add/)).toHaveValue("- added line\n- and a second one");
      expect(within(form).queryByRole("region", { name: "Not known whether it was saved" })).toBeNull();
      expect(within(form).getByText(/newer text is still here/)).toBeInTheDocument();
      expect(within(form).getByRole("button", { name: "Add text" })).toBeEnabled();
    });

    it("journal: \"It is there\" keeps an entry, day or tags changed after the attempt (review 3, item 5)", async () => {
      let fail: (() => void) | null = null;
      renderRoute("/knowledge/vault?write=journal", {
        hub: knowledgeHub({
          "POST /api/action/vault/journal": () =>
            new Promise((_, reject) => {
              fail = () => reject(new TypeError("network connection was lost"));
            }),
        }),
      });
      const form = await screen.findByRole("region", { name: "Add a journal entry" }, { timeout: 12_000 });
      await screen.findByRole("button", { name: "Account: operator" });
      fireEvent.change(within(form).getByLabelText(/^Entry/), { target: { value: "Shipped it" } });
      fireEvent.change(within(form).getByLabelText(/^Day/), { target: { value: "2026-11-10" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add entry" }));
      await waitFor(() => expect(fail).not.toBeNull());
      fireEvent.change(within(form).getByLabelText("Tags"), { target: { value: "release" } });
      await act(async () => fail?.());
      const panel = await within(form).findByRole("region", { name: "Not known whether it was saved" });
      fireEvent.click(within(panel).getByRole("button", { name: "It is there" }));
      expect(within(form).getByLabelText(/^Entry/)).toHaveValue("Shipped it");
      expect(within(form).getByLabelText("Tags")).toHaveValue("release");
      expect(within(form).getByLabelText(/^Day/)).toHaveValue("2026-11-10");
    });

    it("journal: the same two choices, linking that day's journal", async () => {
      const { hub } = renderRoute("/knowledge/vault?write=journal", { hub: knowledgeHub({ "POST /api/action/vault/journal": lost }) });
      const form = await screen.findByRole("region", { name: "Add a journal entry" }, { timeout: 12_000 });
      await screen.findByRole("button", { name: "Account: operator" });
      fireEvent.change(within(form).getByLabelText(/^Entry/), { target: { value: "Shipped it" } });
      fireEvent.change(within(form).getByLabelText(/^Day/), { target: { value: "2026-11-10" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add entry" }));
      const panel = await within(form).findByRole("region", { name: "Not known whether it was saved" });
      expect(within(panel).getByRole("link", { name: "Open the journal for 2026-11-10 and look for your entry" })).toHaveAttribute(
        "href",
        "/ui/knowledge/vault?path=Logs%2F2026-11-10.md",
      );
      expect(within(form).getByRole("button", { name: "Add entry" })).toBeDisabled();
      expect(posts(hub.calls, "/api/action/vault/journal")).toHaveLength(1);
    });

    it("an approved append run from the store whose answer is lost keeps the form blocked", async () => {
      let n = 0;
      const { hub, form } = await sendAppend({
        "GET /api/whoami": { body: whoamiFixture({ org: "org_7f3", org_mode: "partitioned" } as Partial<ReturnType<typeof whoamiFixture>>) },
        "GET /api/approvals/77": { body: { id: 77, tool: "vault_write", status: "approved" } },
        "POST /api/action/vault/write": () => (++n === 1 ? PARKED("vault_write") : lost()),
      });
      fireEvent.click(await within(form).findByRole("button", { name: "Run it now" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/vault/write")).toHaveLength(2));
      expect(await within(form).findByText(/Outcome unknown/)).toBeInTheDocument();
      expect(within(form).getByRole("button", { name: "Add text" })).toBeDisabled();
    });
  });

  it("counts a journal entry live after whitespace collapses and refuses one over 240 characters", async () => {
    const { hub } = renderRoute("/knowledge/vault?write=journal", { hub: knowledgeHub({ "POST /api/action/vault/journal": { body: { path: "Logs/2026-11-10.md", entry: "x", created_note: false } } }) });
    const form = await screen.findByRole("region", { name: "Add a journal entry" });
    const entry = within(form).getByLabelText(/^Entry/);
    fireEvent.change(entry, { target: { value: "Shipped   the\n\nvault   page" } });
    expect(within(form).getByText(/^22 of 240 characters/)).toBeInTheDocument();
    fireEvent.change(entry, { target: { value: "a".repeat(241) } });
    expect(within(form).getByText("1 characters over the limit.")).toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Add entry" }));
    expect(await within(form).findByText(/The entry is 241 characters/)).toBeInTheDocument();
    expect(posts(hub.calls, "/api/action/vault/journal")).toHaveLength(0);
    fireEvent.change(entry, { target: { value: "Shipped the vault page" } });
    fireEvent.change(within(form).getByLabelText(/^Day/), { target: { value: "2026-11-10" } });
    fireEvent.change(within(form).getByLabelText("Tags"), { target: { value: "web, cc2" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add entry" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/vault/journal")[0]?.body).toEqual({ entry: "Shipped the vault page", date: "2026-11-10", tags: ["web", "cc2"] }),
    );
  });

  it("a parked journal entry waits for approval and keeps the entry", async () => {
    renderRoute("/knowledge/vault?write=journal", { hub: knowledgeHub({
        "POST /api/action/vault/journal": PARKED("vault_journal", 81),
        "GET /api/approvals/81": { body: { id: 81, tool: "vault_journal", status: "pending" } },
      }) });
    const form = await screen.findByRole("region", { name: "Add a journal entry" });
    fireEvent.change(within(form).getByLabelText(/^Entry/), { target: { value: "Pending entry" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add entry" }));
    const notice = await within(form).findByRole("region", { name: "Approval status" });
    expect(within(notice).getByText(/Waiting for approval/)).toBeInTheDocument();
    expect(within(notice).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=81");
    expect(within(form).getByLabelText(/^Entry/)).toHaveValue("Pending entry");
    expect(listParkedWrites(fixtureEpoch())[0]).toMatchObject({ path: "/api/action/vault/journal", approvalId: "81" });
    expect((listParkedWrites(fixtureEpoch())[0].body as { date: string }).date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("keeps a vault draft across navigation (defect 8)", async () => {
    const { router } = renderRoute("/knowledge/vault?write=new", { hub: knowledgeHub() });
    const form = await screen.findByRole("region", { name: "New vault note" });
    // Drafts are stored under the signed-in identity, once it is known.
    await screen.findByRole("button", { name: "Account: operator" });
    fireEvent.change(within(form).getByLabelText(/^Note name/), { target: { value: "Draft name" } });
    await act(async () => {
      await router.navigate("/knowledge/memory");
    });
    await findPageHeading("Memory");
    await act(async () => {
      await router.navigate("/knowledge/vault?write=new");
    });
    const again = await screen.findByRole("region", { name: "New vault note" });
    expect(within(again).getByLabelText(/^Note name/)).toHaveValue("Draft name");
  });
});
