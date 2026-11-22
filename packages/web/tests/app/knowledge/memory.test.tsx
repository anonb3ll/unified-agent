import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bumpAuthGeneration } from "../../../src/app/lib/authGeneration";
import { draftKey, readDraft } from "../../../src/app/lib/drafts";
import { clearParkedWrites, listParkedWrites, runParkedWrite } from "../../../src/app/lib/parkedWrites";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import type { MockHandler } from "../support/fetchMock";
import { manyRows, memoryList, memoryNote, MEMORY_ROWS, NOTE_CONTENT, NOTE_KEY } from "./fixtures";
import { knowledgeHub, PARKED, posts } from "./hub";
import { fixtureEpoch } from "../support/epoch";

vi.setConfig({ testTimeout: 40_000 });

function cleanupAll() {
  cleanup();
  sessionStorage.clear();
}

afterEach(() => {
  // Unmount first: a mounted form flushes its draft on unmount, which would
  // otherwise land in the next test's storage.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const noteUrl = (key = NOTE_KEY, extra = "") => `/knowledge/memory?note=${encodeURIComponent(key)}${extra}`;

describe("knowledge: memory list and filters", () => {
  it("lists notes, and tag, author and project filters live in the URL", async () => {
    const { hub, router } = renderRoute("/knowledge/memory", { hub: knowledgeHub() });
    await findPageHeading("Memory");
    expect(await screen.findByRole("link", { name: /Deploy steward owns hub restarts/ })).toHaveAttribute(
      "href",
      `/ui/knowledge/memory?note=${encodeURIComponent(NOTE_KEY)}`,
    );
    expect(screen.getByText(/3 notes/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Author"), { target: { value: "Desktop_Agent2" } });
    await waitFor(() => expect(router.state.location.search).toBe("?author=Desktop_Agent2"));
    expect(screen.queryByRole("link", { name: /Deploy steward/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Run targeted pytest/ })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "unified-agent" } });
    await waitFor(() => expect(hub.calls.some((c) => c.path === "/api/memory" && c.method === "GET")).toBe(true));
    await waitFor(() => expect(router.state.location.search).toContain("project=unified-agent"));

    fireEvent.change(screen.getByLabelText("Tag"), { target: { value: "pref" } });
    await waitFor(() => expect(router.state.location.search).toContain("tag=pref"));
  });

  it("sends the search and the tag to the hub's hybrid search", async () => {
    const urls: string[] = [];
    renderRoute("/knowledge/memory?q=steward&tag=decision", {
      hub: knowledgeHub({
        "GET /api/memory": ({ url }) => {
          urls.push(url);
          return { body: memoryList([MEMORY_ROWS[0]], "search") };
        },
      }),
    });
    await findPageHeading("Memory");
    expect(await screen.findByText(/Best matches first/)).toBeInTheDocument();
    const params = new URL(urls[0], "http://x").searchParams;
    expect(params.get("q")).toBe("steward");
    expect(params.get("tag")).toBe("decision");
    expect(params.get("limit")).toBe("200");
  });

  it("a capped search says it is limited and never claims no notes match (review 1, finding 5)", async () => {
    renderRoute("/knowledge/memory?q=deploy&author=Mac_Hermes", {
      hub: knowledgeHub({ "GET /api/memory": { body: memoryList(manyRows(80), "search", "deploy") } }),
    });
    await findPageHeading("Memory");
    expect(await screen.findByRole("heading", { name: "None of the top 50 matches fit these filters" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No notes match" })).not.toBeInTheDocument();
    expect(screen.getByText(/More notes may match/)).toBeInTheDocument();
  });

  it("a capped list shows the top matches and says how to see others", async () => {
    renderRoute("/knowledge/memory?q=deploy", { hub: knowledgeHub({ "GET /api/memory": { body: memoryList(manyRows(80), "search", "deploy") } }) });
    expect(await screen.findByText(/Showing the top 50 matches; refine the search to see others/)).toBeInTheDocument();
    expect(screen.queryByText(/50 notes/)).not.toBeInTheDocument();
  });

  it("shows no count while the list is loading and explains a failed read without raw text (defects 4 and 10)", async () => {
    let release: (() => void) | null = null;
    renderRoute("/knowledge/memory", {
      hub: knowledgeHub({
        "GET /api/memory": () =>
          new Promise((resolve) => {
            release = () => resolve({ status: 500, body: { error: "sqlite3.OperationalError: database is locked" } });
          }),
      }),
    });
    await findPageHeading("Memory");
    expect(await screen.findByText(/Loading memory notes/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+ notes?/)).not.toBeInTheDocument();
    await act(async () => release?.());
    expect(await screen.findByRole("heading", { name: "Memory notes could not be loaded" })).toBeInTheDocument();
    expect(screen.getByText("The hub had a problem answering. Try again in a moment.")).toBeVisible();
    expect(screen.getByText(/database is locked/)).not.toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("links waiting memory proposals to the Inbox, with the count only after the read", async () => {
    renderRoute("/knowledge/memory", { hub: knowledgeHub() });
    await findPageHeading("Memory");
    const link = await screen.findByRole("link", { name: "Review in the Inbox" });
    expect(link).toHaveAttribute("href", "/ui/inbox?kind=proposal");
    expect(screen.getByText(/1 proposed change to memory or skills is waiting/)).toBeInTheDocument();
  });
});

describe("knowledge: a memory note", () => {
  it("opens a key with dots and slashes from the query string and renders Markdown, never raw HTML", async () => {
    const { hub } = renderRoute(noteUrl(), { hub: knowledgeHub() });
    await findPageHeading("Memory");
    const heading = await screen.findByRole("heading", { level: 2, name: "Deploy steward owns hub restarts" });
    const section = heading.closest("section") as HTMLElement;
    const call = hub.calls.find((c) => c.path === "/api/memory/note");
    expect(call).toBeDefined();
    expect(within(section).getByRole("heading", { level: 3, name: "Who restarts the hub" })).toBeInTheDocument();
    expect(within(section).getByText("deploy steward").tagName).toBe("STRONG");
    expect(section.querySelector("script")).toBeNull();
    expect(section.querySelector("img")).toBeNull();
    expect(within(section).getByText(/<script>alert\(2\)<\/script>/)).toBeInTheDocument();
    // Tags, author, dates and provenance.
    expect(within(screen.getByRole("list", { name: "Tags" })).getByText("decision")).toBeInTheDocument();
    const details = screen.getByRole("region", { name: "Details" });
    expect(within(details).getByText("Machine linux-desktop")).toBeInTheDocument();
    expect(within(details).getByText("observed")).toBeInTheDocument();
    expect(within(details).getByText("PR #1495")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run targeted pytest only on the desktop" })).toHaveAttribute("href", "/ui/knowledge/memory?note=b41c09e2aa10");
    expect(screen.getByText(/Version 1, written/)).toBeInTheDocument();
  });

  it("explains an unknown key", async () => {
    renderRoute(noteUrl("missing.key"), { hub: knowledgeHub({ "GET /api/memory/note": { status: 404, body: { error: "no memory note with id 'missing.key'" } } }) });
    expect(await screen.findByRole("heading", { name: "This note does not exist" })).toBeInTheDocument();
  });

  it("deletes through a confirm dialog, and a deleted note restores as a copy", async () => {
    const { hub } = renderRoute(noteUrl(), {
      hub: knowledgeHub({ "POST /api/action/memory/delete": { body: { deleted: true } } }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this memory note?" });
    expect(within(dialog).getByText(/you can restore a copy later/)).toBeInTheDocument();
    expect(posts(hub.calls, "/api/action/memory/delete")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/delete")[0]?.body).toEqual({ key: NOTE_KEY }));
  });

  it("a deleted note offers to restore a copy of its last version", async () => {
    const { hub, router } = renderRoute(noteUrl(), {
      hub: knowledgeHub({
        "GET /api/memory/note": { body: memoryNote({ deleted: true, tags: ["decision", "meta:deleted"], content: "> deleted by operator" }) },
        "POST /api/action/memory/save": { body: { id: "new0001", created: true, similar: [] } },
      }),
    });
    expect(await screen.findByText(/This note was deleted/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore a copy" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/memory/save")[0]?.body).toEqual({
        content: "Anyone may restart the hub.",
        title: "Deploy steward owns hub restarts",
        tags: ["decision"],
        idempotency_key: expect.stringMatching(/^memory-restore-copy-/),
      }),
    );
    await waitFor(() => expect(router.state.location.search).toBe("?note=new0001"));
  });

  it("restores a note a newer note replaced", async () => {
    const { hub } = renderRoute(noteUrl(), {
      hub: knowledgeHub({
        "GET /api/memory/note": { body: memoryNote({ superseded_by: "c99a01f3bb22" }) },
        "POST /api/action/memory/restore": { body: { id: NOTE_KEY, restored: true, was_superseded_by: "c99a01f3bb22" } },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Bring it back into search" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/restore")[0]?.body).toEqual({ key: NOTE_KEY }));
    expect(await screen.findByText("The note is back in lists and search.")).toBeInTheDocument();
  });

  it("records feedback", async () => {
    const { hub } = renderRoute(noteUrl(), { hub: knowledgeHub({ "POST /api/action/memory/feedback": { body: { id: NOTE_KEY, signal: "wrong" } } }) });
    fireEvent.click(await screen.findByRole("button", { name: "Helpful (2)" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/feedback")[0]?.body).toEqual({ key: NOTE_KEY, signal: "helpful" }));
    fireEvent.click(screen.getByRole("button", { name: "Wrong (0)" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/feedback")[1]?.body).toEqual({ key: NOTE_KEY, signal: "wrong" }));
    expect(await screen.findByText(/Marked as wrong/)).toBeInTheDocument();
  });

  it("previews and promotes a note to the Wiki", async () => {
    const { hub } = renderRoute(noteUrl(), {
      hub: knowledgeHub({
        "POST /api/action/memory/promote": ({ body }) =>
          (body as { dry_run?: boolean }).dry_run
            ? { body: { promoted: false, dry_run: true, path: "Wiki/Learnings/deploy.md", markdown: "---\ntype: promoted-learning\n---\n\n# Deploy steward\n\nBody", index_line: "- x" } }
            : { body: { promoted: true, path: "Wiki/Learnings/deploy.md", wiki_link: "Wiki/Learnings/deploy", index_updated: true } },
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Promote to Wiki" }));
    const dialog = await screen.findByRole("dialog", { name: "Promote this note to the Wiki" });
    fireEvent.change(within(dialog).getByLabelText("Page name"), { target: { value: "deploy" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/promote")[0]?.body).toEqual({ key: NOTE_KEY, slug: "deploy", dry_run: true }));
    expect(await within(dialog).findByRole("heading", { name: "Deploy steward" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Promote" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/promote")[1]?.body).toEqual({ key: NOTE_KEY, slug: "deploy" }));
    expect(await within(dialog).findByRole("link", { name: "Open Wiki/Learnings/deploy.md in the vault" })).toHaveAttribute(
      "href",
      "/ui/knowledge/vault?path=Wiki%2FLearnings%2Fdeploy.md",
    );
  });
});

describe("knowledge: memory writes", () => {
  it("keeps one idempotency key per attempt across clicks, and text typed after submitting survives the success", async () => {
    let release: (() => void) | null = null;
    const { hub, router } = renderRoute("/knowledge/memory?new=1", {
      hub: knowledgeHub({
        "POST /api/action/memory/save": ({ body }) =>
          (body as { content: string }).content === "first"
            ? { status: 500, body: { error: "boom" } }
            : new Promise((resolve) => {
                release = () => resolve({ body: { id: "k1", created: true } });
              }),
      }),
    });
    const form = await screen.findByRole("region", { name: "New memory note" });
    const text = within(form).getByLabelText(/^Text/);
    fireEvent.change(text, { target: { value: "first" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
    await within(form).findByRole("alert");
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(2));
    const [a, b] = posts(hub.calls, "/api/action/memory/save").map((c) => (c.body as { idempotency_key: string }).idempotency_key);
    expect(a).toBe(b);
    fireEvent.change(text, { target: { value: "second" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(3));
    expect((posts(hub.calls, "/api/action/memory/save")[2].body as { idempotency_key: string }).idempotency_key).not.toBe(a);
    // Typed while the save was in flight: kept, not cleared by the success.
    fireEvent.change(text, { target: { value: "second, and more" } });
    await act(async () => release?.());
    expect(await within(form).findByText(/your newer text is still here/)).toBeInTheDocument();
    expect(text).toHaveValue("second, and more");
    expect(router.state.location.search).toBe("?new=1");
  });

  it("returns focus to Edit when the editor is cancelled", async () => {
    renderRoute(noteUrl(NOTE_KEY, "&edit=1"), { hub: knowledgeHub() });
    const form = await screen.findByRole("region", { name: /Edit/ });
    fireEvent.click(within(form).getByRole("link", { name: "Cancel" }));
    const edit = await screen.findByRole("link", { name: "Edit" });
    await waitFor(() => expect(edit).toHaveFocus());
  });

  it("after a failed refetch the list says so, with Retry, over the last good data", async () => {
    let fail = false;
    const { appClient } = renderRoute("/knowledge/memory", {
      hub: knowledgeHub({ "GET /api/memory": () => (fail ? { status: 500, body: { error: "locked" } } : { body: memoryList() }) }),
    });
    await screen.findByRole("link", { name: /Deploy steward owns hub restarts/ });
    fail = true;
    await act(async () => {
      await appClient().refetchQueries({ queryKey: ["knowledge", "memory", "list"] });
    });
    const alert = await screen.findByText(/What you see may be out of date/);
    expect(alert.closest("[role=alert]")).not.toBeNull();
    expect(screen.getByRole("link", { name: /Deploy steward owns hub restarts/ })).toBeInTheDocument();
  });

  it("creates a note and opens it", async () => {
    const { hub, router } = renderRoute("/knowledge/memory?new=1", { hub: knowledgeHub({ "POST /api/action/memory/save": { body: { id: "a1b2c3d4e5f6", created: true, similar: [] } } }) });
    const form = await screen.findByRole("region", { name: "New memory note" });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    expect(await within(form).findByText("Write the note's text.")).toBeInTheDocument();
    expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Hub restarts" } });
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Only the steward restarts it." } });
    fireEvent.change(within(form).getByLabelText("Tags"), { target: { value: "project:unified-agent, decision" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
    const body = posts(hub.calls, "/api/action/memory/save")[0].body as Record<string, unknown>;
    expect(body).toMatchObject({ content: "Only the steward restarts it.", title: "Hub restarts", tags: ["project:unified-agent", "decision"] });
    expect(body.idempotency_key).toMatch(/^memory-create-/);
    expect(body).not.toHaveProperty("author");
    await waitFor(() => expect(router.state.location.search).toBe("?note=a1b2c3d4e5f6"));
  });

  it("a parked save says it waits for approval, links the Inbox and keeps the form (defects 1 and 9)", async () => {
    const { hub } = renderRoute("/knowledge/memory?new=1", {
      hub: knowledgeHub({
        "POST /api/action/memory/save": PARKED("memory_save"),
        "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "pending" } },
      }),
    });
    const form = await screen.findByRole("region", { name: "New memory note" });
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Parked text" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
    const notice = await within(form).findByRole("region", { name: "Approval status" });
    expect(within(notice).getByText(/Waiting for approval/)).toBeInTheDocument();
    expect(within(notice).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=77");
    expect(notice).not.toHaveTextContent(/by itself|automatically/);
    expect(within(form).getByLabelText(/^Text/)).toHaveValue("Parked text");
    expect(screen.queryByText("Note saved.")).not.toBeInTheDocument();
    // Resumable: the exact body is stored for the shell's runner to repeat once approved.
    const parked = listParkedWrites(fixtureEpoch());
    expect(parked).toHaveLength(1);
    expect(parked[0]).toMatchObject({ path: "/api/action/memory/save", method: "POST", approvalId: "77", body: posts(hub.calls, "/api/action/memory/save")[0].body });
  });

  describe("after approval the person runs the parked save, and the form reconciles (review 1, finding 4)", () => {
    const ORG_WHOAMI = { body: whoamiFixture({ org: "org_7f3", org_mode: "partitioned" } as Partial<ReturnType<typeof whoamiFixture>>) };
    function approvedHub(saveCount: { n: number }) {
      return knowledgeHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "approved" } },
        "POST /api/action/memory/save": () => (++saveCount.n === 1 ? PARKED("memory_save") : { body: { id: "ran0001", created: true } }),
      });
    }

    it("Run it now repeats the identical request; the applied text is cleared and the note opens", async () => {
      const count = { n: 0 };
      const { hub, router } = renderRoute("/knowledge/memory?new=1", { hub: approvedHub(count) });
      const form = await screen.findByRole("region", { name: "New memory note" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Approved text" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      const run = await within(form).findByRole("button", { name: "Run it now" });
      fireEvent.click(run);
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(2));
      const [first, second] = posts(hub.calls, "/api/action/memory/save");
      expect(second.body).toEqual(first.body);
      await waitFor(() => expect(router.state.location.search).toBe("?note=ran0001"));
      await act(async () => {
        await router.navigate("/knowledge/memory?new=1");
      });
      const again = await screen.findByRole("region", { name: "New memory note" });
      expect(within(again).getByLabelText(/^Text/)).toHaveValue("");
    });

    it("text typed after the submit is kept when the approved save runs", async () => {
      const count = { n: 0 };
      const { router } = renderRoute("/knowledge/memory?new=1", { hub: approvedHub(count) });
      const form = await screen.findByRole("region", { name: "New memory note" });
      const text = within(form).getByLabelText(/^Text/);
      fireEvent.change(text, { target: { value: "Approved text" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      const run = await within(form).findByRole("button", { name: "Run it now" });
      fireEvent.change(text, { target: { value: "Approved text, then more" } });
      fireEvent.click(run);
      expect(await within(form).findByText(/your newer text is still here/)).toBeInTheDocument();
      expect(text).toHaveValue("Approved text, then more");
      expect(router.state.location.search).toBe("?new=1");
    });

    it("a confirmed run retires the attempt key, so the same text later is a new attempt (review 2, item 9)", async () => {
      let saves = 0;
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: knowledgeHub({
          "GET /api/whoami": ORG_WHOAMI,
          "GET /api/memory/note": { body: memoryNote({ author: "operator" }) },
          "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "approved" } },
          "POST /api/action/memory/save": () => (++saves === 1 ? PARKED("memory_save") : { body: { id: NOTE_KEY, created: false } }),
        }),
      });
      const form = await screen.findByRole("region", { name: /Edit/ }, { timeout: 12_000 });
      fireEvent.click(within(form).getByRole("radio", { name: /Add to the end/ }));
      const addition = within(form).getByLabelText(/^Text to add/);
      fireEvent.change(addition, { target: { value: "Same line" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
      const run = await within(form).findByRole("button", { name: "Run it now" });
      fireEvent.change(addition, { target: { value: "Same line, typed on" } });
      fireEvent.click(run);
      await within(form).findByText(/your newer text is still here/);
      fireEvent.change(addition, { target: { value: "Same line" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(3));
      const keys = posts(hub.calls, "/api/action/memory/save").map((c) => (c.body as { idempotency_key: string }).idempotency_key);
      expect(keys[1]).toBe(keys[0]);
      expect(keys[2]).not.toBe(keys[0]);
    });

    it("an approved promotion run clears the dialog's draft (review 2, item 10)", async () => {
      let promotes = 0;
      const { hub } = renderRoute(noteUrl(), {
        hub: knowledgeHub({
          "GET /api/whoami": ORG_WHOAMI,
          "GET /api/approvals/77": { body: { id: 77, tool: "memory_promote", status: "approved" } },
          "POST /api/action/memory/promote": () =>
            ++promotes === 1
              ? PARKED("memory_promote")
              : { body: { promoted: true, dry_run: false, path: "Wiki/Learnings/deploy.md", wiki_link: "Wiki/Learnings/deploy", index_updated: true } },
        }),
      });
      fireEvent.click(await screen.findByRole("button", { name: "Promote to Wiki" }, { timeout: 12_000 }));
      const dialog = await screen.findByRole("dialog", { name: "Promote this note to the Wiki" });
      fireEvent.change(within(dialog).getByLabelText("Page name"), { target: { value: "deploy" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Promote" }));
      fireEvent.click(await within(dialog).findByRole("button", { name: "Run it now" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/promote")).toHaveLength(2));
      expect(posts(hub.calls, "/api/action/memory/promote")[1].body).toEqual(posts(hub.calls, "/api/action/memory/promote")[0].body);
      await waitFor(() => expect(within(dialog).getByLabelText("Page name")).toHaveValue(""));
    });

    it("a run that parks again moves the form to the new approval, here and after a remount (review 3, item 6)", async () => {
      let saves = 0;
      const hubFor = () =>
        knowledgeHub({
          "GET /api/whoami": ORG_WHOAMI,
          "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "approved" } },
          "GET /api/approvals/78": { body: { id: 78, tool: "memory_save", status: "pending" } },
          "POST /api/action/memory/save": () => (++saves === 1 ? PARKED("memory_save", 77) : PARKED("memory_save", 78)),
        });
      const first = renderRoute("/knowledge/memory?new=1", { hub: hubFor() });
      const form = await screen.findByRole("region", { name: "New memory note" }, { timeout: 12_000 });
      await screen.findByRole("button", { name: "Account: operator" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Needs two approvals" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      fireEvent.click(await within(form).findByRole("button", { name: "Run it now" }));
      await waitFor(() => expect(within(form).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=78"));
      first.unmount();
      renderRoute("/knowledge/memory?new=1", { hub: hubFor() });
      const again = await screen.findByRole("region", { name: "New memory note" });
      await waitFor(() => expect(within(again).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=78"));
    });

    it("a run from the Inbox that parks again is followed when the form comes back (review 3, item 6)", async () => {
      let saves = 0;
      const hub = knowledgeHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "approved" } },
        "GET /api/approvals/78": { body: { id: 78, tool: "memory_save", status: "pending" } },
        "POST /api/action/memory/save": () => (++saves === 1 ? PARKED("memory_save", 77) : PARKED("memory_save", 78)),
      });
      const { router, epoch } = renderRoute("/knowledge/memory?new=1", { hub });
      const form = await screen.findByRole("region", { name: "New memory note" }, { timeout: 12_000 });
      await screen.findByRole("button", { name: "Account: operator" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Needs two approvals" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      await within(form).findByRole("button", { name: "Run it now" });
      await act(async () => {
        await router.navigate("/knowledge/vault");
      });
      await act(async () => {
        const result = await runParkedWrite(listParkedWrites(epoch())[0].id, { epoch: epoch() });
        expect(result.kind).toBe("pending_approval");
      });
      await act(async () => {
        await router.navigate("/knowledge/memory?new=1");
      });
      const again = await screen.findByRole("region", { name: "New memory note" });
      await waitFor(() => expect(within(again).getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=78"));
    });

    it("an approved restore-copy run retires its key, so another copy is a new request (review 3, item 9)", async () => {
      let saves = 0;
      const deletedNote = memoryNote({ deleted: true, tags: ["decision", "meta:deleted"], content: "tombstone" });
      const handlers = knowledgeHub({
        "GET /api/whoami": ORG_WHOAMI,
        "GET /api/memory/note": { body: deletedNote },
        "GET /api/approvals/77": { body: { id: 77, tool: "memory_save", status: "approved" } },
        "POST /api/action/memory/save": () => (++saves === 1 ? PARKED("memory_save") : { body: { id: `copy${saves}`, created: true } }),
      });
      const { router, hub } = renderRoute(noteUrl(), { hub: handlers });
      await screen.findByRole("button", { name: "Account: operator" }, { timeout: 12_000 });
      fireEvent.click(await screen.findByRole("button", { name: "Restore a copy" }));
      fireEvent.click(await screen.findByRole("button", { name: "Run it now" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(2));
      // Back on the deleted note, ask for another copy.
      await act(async () => {
        await router.navigate(noteUrl());
      });
      fireEvent.click(await screen.findByRole("button", { name: "Restore a copy" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(3));
      const keys = posts(hub.calls, "/api/action/memory/save").map((c) => (c.body as { idempotency_key: string }).idempotency_key);
      expect(keys[1]).toBe(keys[0]);
      expect(keys[2]).not.toBe(keys[0]);
    });

    // Task #2525: the stored write carries the draft binding; the draft keeps no copy of the parked text.
    const CREATE_DRAFT = draftKey({ identity: "operator", operation: "memory.create", object: "" });

    it("the run clears the stored draft while the form is away, without the form mounting (#2525)", async () => {
      const count = { n: 0 };
      const { router, epoch } = renderRoute("/knowledge/memory?new=1", { hub: approvedHub(count) });
      const form = await screen.findByRole("region", { name: "New memory note" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Approved text" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      await within(form).findByRole("button", { name: "Run it now" });
      const stored = listParkedWrites(epoch())[0];
      expect(stored.draft).toEqual({ draftKey: CREATE_DRAFT, submittedSnapshot: { title: "", content: "Approved text", tags: "", confidence: "" } });
      await act(async () => {
        await router.navigate("/knowledge/vault");
      });
      const kept = readDraft<Record<string, unknown>>(epoch(), CREATE_DRAFT);
      expect(kept).toMatchObject({ content: "Approved text", parkedApprovalId: "77" });
      expect(kept).not.toHaveProperty("parkedSnapshot");
      await act(async () => {
        expect((await runParkedWrite(stored.id, { epoch: epoch() })).kind).toBe("done");
      });
      expect(readDraft(epoch(), CREATE_DRAFT)).toBeNull();
    });

    it("text typed after the submit survives a run while the form is away, and says so on return (#2525)", async () => {
      const count = { n: 0 };
      const { router, epoch } = renderRoute("/knowledge/memory?new=1", { hub: approvedHub(count) });
      const form = await screen.findByRole("region", { name: "New memory note" });
      const text = within(form).getByLabelText(/^Text/);
      fireEvent.change(text, { target: { value: "Approved text" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      await within(form).findByRole("button", { name: "Run it now" });
      fireEvent.change(text, { target: { value: "Approved text, then more" } });
      await act(async () => {
        await router.navigate("/knowledge/vault");
      });
      await act(async () => {
        expect((await runParkedWrite(listParkedWrites(epoch())[0].id, { epoch: epoch() })).kind).toBe("done");
      });
      expect(readDraft(epoch(), CREATE_DRAFT)).toMatchObject({ content: "Approved text, then more" });
      await act(async () => {
        await router.navigate("/knowledge/memory?new=1");
      });
      const again = await screen.findByRole("region", { name: "New memory note" });
      expect(within(again).getByLabelText(/^Text/)).toHaveValue("Approved text, then more");
      expect(await within(again).findByText(/your newer text is still here/)).toBeInTheDocument();
      expect(within(again).queryByRole("region", { name: "Approval status" })).toBeNull();
    });

    it("a run from the Inbox while the form was away does not restore the applied text", async () => {
      const count = { n: 0 };
      const { router, epoch } = renderRoute("/knowledge/memory?new=1", { hub: approvedHub(count) });
      const form = await screen.findByRole("region", { name: "New memory note" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Approved text" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
      await within(form).findByRole("button", { name: "Run it now" });
      await act(async () => {
        await router.navigate("/knowledge/vault");
      });
      const stored = listParkedWrites(epoch())[0];
      await act(async () => {
        const result = await runParkedWrite(stored.id, { epoch: epoch() });
        expect(result.kind).toBe("done");
      });
      await act(async () => {
        await router.navigate("/knowledge/memory?new=1");
      });
      const again = await screen.findByRole("region", { name: "New memory note" });
      await waitFor(() => expect(within(again).getByLabelText(/^Text/)).toHaveValue(""));
    });
  });

  it("a 200 that carries an approval id is a success, not a parked write", async () => {
    renderRoute("/knowledge/memory?new=1", { hub: knowledgeHub({ "POST /api/action/memory/save": { body: { id: "z9", created: true, approval_id: 5 } } }) });
    const form = await screen.findByRole("region", { name: "New memory note" });
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    expect(await screen.findByText("Note saved.")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for approval")).not.toBeInTheDocument();
  });

  it("keeps a draft across navigation and restores it (defect 8)", async () => {
    const { router } = renderRoute("/knowledge/memory?new=1", { hub: knowledgeHub() });
    const form = await screen.findByRole("region", { name: "New memory note" });
    // Drafts are stored under the signed-in identity, once it is known.
    await within(form).findByText(/saved under your name \(operator\)/);
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Half-written thought" } });
    await act(async () => {
      await router.navigate("/knowledge/vault");
    });
    await findPageHeading("Vault");
    await act(async () => {
      await router.navigate("/knowledge/memory?new=1");
    });
    const again = await screen.findByRole("region", { name: "New memory note" });
    expect(within(again).getByLabelText(/^Text/)).toHaveValue("Half-written thought");
    expect(within(again).getByText("Your unsaved draft was restored.")).toBeInTheDocument();
  });

  it("editing another author's note warns and needs the explicit overwrite choice", async () => {
    const { hub, router } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
      hub: knowledgeHub({
        "GET /api/whoami": { body: whoamiFixture({ agent: "operator" }) },
        "POST /api/action/memory/save": { body: { id: NOTE_KEY, created: false } },
      }),
    });
    const form = await screen.findByRole("region", { name: /Edit “Deploy steward owns hub restarts”/ });
    expect(within(form).getByText("Desktop_Agent1 wrote this note, not you.")).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Replaced text" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await within(form).findByText(/Tick the box to confirm/)).toBeInTheDocument();
    expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    fireEvent.click(within(form).getByRole("checkbox", { name: "Replace the note Desktop_Agent1 wrote" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/memory/save")[0]?.body).toEqual({
        key: NOTE_KEY,
        content: "Replaced text",
        title: "Deploy steward owns hub restarts",
        tags: ["project:unified-agent", "decision"],
        overwrite: true,
        idempotency_key: expect.stringMatching(/^memory-edit-/),
      }),
    );
    await waitFor(() => expect(router.state.location.search).toBe(`?note=${encodeURIComponent(NOTE_KEY)}`));
  });

  describe("a replace never removes a change made after the editor opened (review 1 #1, review 2)", () => {
    const APPENDED = `${NOTE_CONTENT}\n\n> Appended by Desktop_Agent2: also on Fridays.`;
    function changingHub(extra: Record<string, MockHandler> = {}) {
      let reads = 0;
      return knowledgeHub({
        "GET /api/whoami": { body: whoamiFixture({ agent: "Desktop_Agent1" }) },
        "GET /api/memory/note": () =>
          ++reads === 1
            ? { body: memoryNote() }
            : { body: memoryNote({ content: APPENDED, updated_at: "2026-11-10T20:00:00+00:00", updated_by: "Desktop_Agent2" }) },
        "POST /api/action/memory/save": { body: { id: NOTE_KEY, created: false } },
        ...extra,
      });
    }
    async function editAndSave() {
      const form = await screen.findByRole("region", { name: /Edit/ });
      await screen.findByRole("button", { name: "Account: Desktop_Agent1" });
      const text = within(form).getByLabelText(/^Text/);
      fireEvent.change(text, { target: { value: `${NOTE_CONTENT}\n\nMy line.` } });
      fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
      return form;
    }

    it("saves nothing and shows the note now beside my edit, with exactly two choices", async () => {
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), { hub: changingHub() });
      const form = await editAndSave();
      const panel = await within(form).findByRole("region", { name: "The note changed after you opened it" });
      expect(within(panel).getByText(/Desktop_Agent2 changed it/)).toBeInTheDocument();
      expect(within(panel).getByRole("region", { name: "The note now" })).toHaveTextContent("Appended by Desktop_Agent2");
      expect(within(panel).getByRole("region", { name: "Your edit" })).toHaveTextContent("My line.");
      expect(within(panel).getAllByRole("button").map((b) => b.textContent)).toEqual(["Keep editing from the latest version", "Replace with my version"]);
      expect(within(panel).queryByRole("button", { name: /re-apply|Append my change/ })).toBeNull();
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    });

    it("keep editing loads the latest version into every field and keeps my edit, across a remount", async () => {
      const changed = { content: APPENDED, title: "Renamed", tags: ["decision"], confidence: 0.7, updated_at: "2026-11-10T20:00:00+00:00", updated_by: "Desktop_Agent2" };
      const first = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), { hub: changingHub({ "GET /api/memory/note": (() => {
        let reads = 0;
        return () => (++reads === 1 ? { body: memoryNote() } : { body: memoryNote(changed) });
      })() }) });
      const form = await editAndSave();
      fireEvent.click(await within(form).findByRole("button", { name: "Keep editing from the latest version" }));
      expect(within(form).getByLabelText(/^Text/)).toHaveValue(APPENDED);
      expect(within(form).getByLabelText("Title")).toHaveValue("Renamed");
      expect(within(form).getByLabelText("Tags")).toHaveValue("decision");
      expect(within(form).getByLabelText("How sure is this note?")).toHaveValue("0.7");
      expect(within(form).getByRole("region", { name: /^Your earlier edit/ })).toHaveTextContent("My line.");
      first.unmount();
      renderRoute(noteUrl(NOTE_KEY, "&edit=1"), { hub: changingHub({ "GET /api/memory/note": { body: memoryNote(changed) } }) });
      const again = await screen.findByRole("region", { name: /Edit/ });
      const earlier = await within(again).findByRole("region", { name: /^Your earlier edit/ });
      expect(earlier).toHaveTextContent("My line.");
      fireEvent.click(within(earlier).getByRole("button", { name: /Dismiss/ }));
      expect(within(again).queryByRole("region", { name: /^Your earlier edit/ })).toBeNull();
    });

    it("a very large note shows the two versions without a line diff", async () => {
      const big = Array.from({ length: 3000 }, (_, i) => `line ${i}`).join("\n");
      let reads = 0;
      renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({ "GET /api/memory/note": () => (++reads === 1 ? { body: memoryNote({ content: big }) } : { body: memoryNote({ content: `${big}\nmore`, updated_at: "2026-11-10T20:00:00+00:00" }) }) }),
      });
      const form = await screen.findByRole("region", { name: /Edit/ });
      await screen.findByRole("button", { name: "Account: Desktop_Agent1" });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: `${big}\nmine` } });
      fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
      const panel = await within(form).findByRole("region", { name: "The note changed after you opened it" });
      expect(within(panel).queryByRole("region", { name: "Lines that differ" })).toBeNull();
      expect(within(panel).getByText(/too long to compare line by line/i)).toBeInTheDocument();
      expect(within(panel).getByText(/Text differs/)).toBeInTheDocument();
    });

    it("replace with my version needs a second confirm naming what is overwritten, then re-reads before saving", async () => {
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), { hub: changingHub() });
      const form = await editAndSave();
      fireEvent.click(await within(form).findByRole("button", { name: "Replace with my version" }));
      const dialog = await screen.findByRole("dialog", { name: "Replace the note with your version?" });
      expect(dialog).toHaveTextContent(/Desktop_Agent2/);
      expect(dialog).toHaveTextContent(/text/);
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
      fireEvent.click(within(dialog).getByRole("button", { name: "Replace with my version" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")[0]?.body).toMatchObject({ key: NOTE_KEY, content: `${NOTE_CONTENT}\n\nMy line.` }));
    });

    it("a failed re-read saves nothing", async () => {
      let reads = 0;
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({ "GET /api/memory/note": () => (++reads === 1 ? { body: memoryNote() } : { status: 500, body: { error: "locked" } }) }),
      });
      const form = await editAndSave();
      expect(await within(form).findByText(/Nothing was saved: the note could not be re-read/)).toBeInTheDocument();
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    });

    it("a sign-out during the pre-save read sends nothing and leaves the draft alone (review 2, blocker 1)", async () => {
      let reads = 0;
      let release: (() => void) | null = null;
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({
          "GET /api/memory/note": () =>
            ++reads === 1
              ? { body: memoryNote() }
              : new Promise((resolve) => {
                  release = () => resolve({ body: memoryNote() });
                }),
        }),
      });
      const form = await editAndSave();
      await waitFor(() => expect(release).not.toBeNull());
      await act(async () => {
        bumpAuthGeneration();
        release?.();
      });
      await new Promise((r) => setTimeout(r, 50));
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
      expect(within(form).queryByText(/changed after you opened it/)).toBeNull();
    });

    it("a different identity after the read sends nothing (review 2, blocker 1)", async () => {
      let reads = 0;
      let release: (() => void) | null = null;
      const { hub, queryClient } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({
          "GET /api/memory/note": () =>
            ++reads === 1
              ? { body: memoryNote() }
              : new Promise((resolve) => {
                  release = () => resolve({ body: memoryNote() });
                }),
        }),
      });
      await editAndSave();
      await waitFor(() => expect(release).not.toBeNull());
      await act(async () => {
        queryClient.setQueryData(["whoami"], whoamiFixture({ agent: "Mac_Hermes" }));
        queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "Mac_Hermes" }));
        release?.();
      });
      await new Promise((r) => setTimeout(r, 50));
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    });

    it("text typed while the pre-save read waits is kept, and success does not clear it (review 3, item 1)", async () => {
      let reads = 0;
      let release: (() => void) | null = null;
      const { hub, router } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({
          // Only the pre-save read (the second) waits; the refetch after the save answers at once.
          "GET /api/memory/note": () =>
            ++reads === 2
              ? new Promise((resolve) => {
                  release = () => resolve({ body: memoryNote() });
                })
              : { body: memoryNote() },
        }),
      });
      const form = await editAndSave();
      await waitFor(() => expect(release).not.toBeNull());
      const text = within(form).getByLabelText(/^Text/);
      fireEvent.change(text, { target: { value: `${NOTE_CONTENT}\n\nMy line.\nTyped during the read.` } });
      await act(async () => release?.());
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
      // The request carries what was submitted; the field keeps what was typed since.
      expect((posts(hub.calls, "/api/action/memory/save")[0].body as { content: string }).content).toBe(`${NOTE_CONTENT}\n\nMy line.`);
      expect(await within(form).findByText(/newer text is still here/)).toBeInTheDocument();
      expect(text).toHaveValue(`${NOTE_CONTENT}\n\nMy line.\nTyped during the read.`);
      expect(router.state.location.search).toContain("edit=1");
    });

    it("a read started for note A never writes into note B's editor (review 3, item 2)", async () => {
      const OTHER = "b41c09e2aa10";
      let releaseA: (() => void) | null = null;
      let aReads = 0;
      // B is opened first, so its note is cached and its editor shows at once when we return.
      const { hub, router } = renderRoute(noteUrl(OTHER, "&edit=1"), {
        hub: changingHub({
          "GET /api/memory/note": ({ url }) => {
            const key = new URL(url, "http://x").searchParams.get("key");
            if (key === OTHER) return { body: memoryNote({ id: OTHER, title: "Run targeted pytest only on the desktop", content: "B body", author: "Desktop_Agent2" }) };
            if (++aReads === 1) return { body: memoryNote() };
            return new Promise((resolve) => {
              releaseA = () => resolve({ body: memoryNote({ content: APPENDED, updated_at: "2026-11-10T20:00:00+00:00" }) });
            });
          },
        }),
      });
      await screen.findByRole("region", { name: /Edit “Run targeted pytest/ }, { timeout: 12_000 });
      await act(async () => {
        await router.navigate(noteUrl(NOTE_KEY, "&edit=1"));
      });
      await editAndSave();
      await waitFor(() => expect(releaseA).not.toBeNull());
      await act(async () => {
        await router.navigate(noteUrl(OTHER, "&edit=1"));
      });
      const formB = await screen.findByRole("region", { name: /Edit “Run targeted pytest/ });
      await act(async () => releaseA?.());
      await new Promise((r) => setTimeout(r, 50));
      expect(within(formB).getByLabelText(/^Text/)).toHaveValue("B body");
      expect(within(formB).queryByRole("region", { name: "The note changed after you opened it" })).toBeNull();
      expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(0);
    });

    it("a confidence left unchosen is shown as kept, and is not listed as overwritten (review 3, item 7)", async () => {
      let reads = 0;
      renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({
          "GET /api/memory/note": () =>
            ++reads === 1
              ? { body: memoryNote({ confidence: 0.7 }) }
              : { body: memoryNote({ confidence: 0.9, content: APPENDED, updated_at: "2026-11-10T20:00:00+00:00", updated_by: "Desktop_Agent2" }) },
        }),
      });
      const form = await editAndSave();
      const panel = await within(form).findByRole("region", { name: "The note changed after you opened it" });
      const yours = within(panel).getByRole("region", { name: "Your edit" });
      expect(yours).toHaveTextContent(/Confidence\s*Not changed: the note keeps its own/);
      expect(yours).not.toHaveTextContent("0.7");
      expect(within(panel).queryByText(/Confidence differs/)).toBeNull();
      fireEvent.click(within(panel).getByRole("button", { name: "Replace with my version" }));
      const dialog = await screen.findByRole("dialog", { name: "Replace the note with your version?" });
      expect(dialog).not.toHaveTextContent(/confidence/);
      expect(dialog).toHaveTextContent(/Its text will be replaced/);
    });

    it("a second conflict keeps the first set-aside edit too (review 3, item 8)", async () => {
      let reads = 0;
      const versions = [
        memoryNote(),
        memoryNote({ content: `${NOTE_CONTENT}\nfirst change`, updated_at: "2026-11-10T20:00:00+00:00", updated_by: "Desktop_Agent2" }),
        memoryNote({ content: `${NOTE_CONTENT}\nfirst change\nsecond change`, updated_at: "2026-11-10T21:00:00+00:00", updated_by: "Mac_Hermes" }),
      ];
      renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: changingHub({ "GET /api/memory/note": () => ({ body: versions[Math.min(reads++, 2)] }) }),
      });
      const form = await editAndSave();
      fireEvent.click(await within(form).findByRole("button", { name: "Keep editing from the latest version" }));
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Second attempt" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
      fireEvent.click(await within(form).findByRole("button", { name: "Keep editing from the latest version" }));
      const kept = within(form).getAllByRole("region", { name: /^Your earlier edit/ });
      expect(kept).toHaveLength(2);
      expect(kept[0]).toHaveTextContent("Second attempt");
      expect(kept[1]).toHaveTextContent("My line.");
      fireEvent.click(within(kept[0]).getByRole("button", { name: /Dismiss/ }));
      expect(within(form).getAllByRole("region", { name: /^Your earlier edit/ })).toHaveLength(1);
    });

    it("a parked replace is not stored for a later run; a parked append is (review 2, blocker 2)", async () => {
      const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
        hub: knowledgeHub({
          "GET /api/memory/note": { body: memoryNote({ author: "operator" }) },
          "POST /api/action/memory/save": PARKED("memory_save", 91),
          "GET /api/approvals/91": { body: { id: 91, tool: "memory_save", status: "pending" } },
        }),
      });
      const form = await screen.findByRole("region", { name: /Edit/ });
      fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "Replaced" } });
      fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
      await within(form).findByRole("region", { name: "Approval status" });
      expect(listParkedWrites(fixtureEpoch())).toHaveLength(0);
      fireEvent.click(within(form).getByRole("radio", { name: /Add to the end/ }));
      fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "More" } });
      fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
      await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(2));
      await waitFor(() => expect(listParkedWrites(fixtureEpoch())).toHaveLength(1));
      expect(listParkedWrites(fixtureEpoch())[0].body).toMatchObject({ append: true, content: "More" });
    });
  });

  it("confidence is chosen explicitly on create, never set for the person (review 1, finding 6)", async () => {
    const { hub } = renderRoute("/knowledge/memory?new=1", { hub: knowledgeHub({ "POST /api/action/memory/save": { body: { id: "n1", created: true } } }) });
    const form = await screen.findByRole("region", { name: "New memory note" });
    const sure = within(form).getByLabelText("How sure is this note?");
    expect(sure).toHaveValue("");
    fireEvent.change(within(form).getByLabelText(/^Text/), { target: { value: "x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/memory/save")[0].body).not.toHaveProperty("confidence");
    cleanupAll();
    const second = renderRoute("/knowledge/memory?new=1", { hub: knowledgeHub({ "POST /api/action/memory/save": { body: { id: "n2", created: true } } }) });
    const form2 = await screen.findByRole("region", { name: "New memory note" });
    fireEvent.change(within(form2).getByLabelText(/^Text/), { target: { value: "y" } });
    fireEvent.change(within(form2).getByLabelText("How sure is this note?"), { target: { value: "0.9" } });
    fireEvent.click(within(form2).getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts(second.hub.calls, "/api/action/memory/save")[0]?.body).toMatchObject({ content: "y", confidence: 0.9 }));
  });

  it("the editor sets a chosen confidence", async () => {
    const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
      hub: knowledgeHub({ "GET /api/memory/note": { body: memoryNote({ author: "operator", confidence: null }) }, "POST /api/action/memory/save": { body: { id: NOTE_KEY } } }),
    });
    const form = await screen.findByRole("region", { name: /Edit/ });
    fireEvent.change(within(form).getByLabelText("How sure is this note?"), { target: { value: "0.8" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")[0]?.body).toMatchObject({ key: NOTE_KEY, confidence: 0.8 }));
  });

  it("the promote dialog explains how to raise a missing confidence", async () => {
    renderRoute(noteUrl(), { hub: knowledgeHub({ "GET /api/memory/note": { body: memoryNote({ confidence: null }) } }) });
    fireEvent.click(await screen.findByRole("button", { name: "Promote to Wiki" }));
    const dialog = await screen.findByRole("dialog", { name: "Promote this note to the Wiki" });
    expect(within(dialog).getByText(/choose “How sure is this note\?”/)).toBeInTheDocument();
  });

  it("clearing the title sends an empty title, not \"keep\" (review 1, finding 9)", async () => {
    const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
      hub: knowledgeHub({
        "GET /api/memory/note": { body: memoryNote({ author: "operator" }) },
        "POST /api/action/memory/save": { body: { id: NOTE_KEY, created: false } },
      }),
    });
    const form = await screen.findByRole("region", { name: /Edit/ });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(1));
    expect(posts(hub.calls, "/api/action/memory/save")[0].body).toMatchObject({ key: NOTE_KEY, title: "" });
    // Confidence not chosen in the editor: nothing is sent, so the hub keeps the note's own.
    expect(posts(hub.calls, "/api/action/memory/save")[0].body).not.toHaveProperty("confidence");
  });

  it("an edit keeps one idempotency key per attempt across retries and retires it after success (review 1, finding 2)", async () => {
    let calls = 0;
    const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
      hub: knowledgeHub({
        "POST /api/action/memory/save": () => (++calls === 1 ? { status: 500, body: { error: "database is locked" } } : { body: { id: NOTE_KEY, created: false } }),
      }),
    });
    const form = await screen.findByRole("region", { name: /Edit/ });
    fireEvent.click(within(form).getByRole("radio", { name: /Add to the end/ }));
    fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "One more line" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
    await within(form).findByRole("alert");
    fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
    await waitFor(() => expect(posts(hub.calls, "/api/action/memory/save")).toHaveLength(2));
    const keys = posts(hub.calls, "/api/action/memory/save").map((c) => (c.body as { idempotency_key?: string }).idempotency_key);
    expect(keys[0]).toMatch(/^memory-edit-/);
    expect(keys[1]).toBe(keys[0]);
    await waitFor(() => expect(sessionStorage.length).toBe(0));
  });

  it("appends without the overwrite choice, and explains the hub's refusal in plain words", async () => {
    const { hub } = renderRoute(noteUrl(NOTE_KEY, "&edit=1"), {
      hub: knowledgeHub({
        "POST /api/action/memory/save": {
          status: 400,
          body: { error: `memory note '${NOTE_KEY}' is owned by 'Desktop_Agent1'; you are operator. Pass append=True to add an attributed block without destroying it.` },
        },
      }),
    });
    const form = await screen.findByRole("region", { name: /Edit/ });
    fireEvent.click(within(form).getByRole("radio", { name: /Add to the end/ }));
    fireEvent.change(within(form).getByLabelText(/^Text to add/), { target: { value: "One more line" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add text" }));
    await waitFor(() =>
      expect(posts(hub.calls, "/api/action/memory/save")[0]?.body).toEqual({
        key: NOTE_KEY,
        content: "One more line",
        append: true,
        idempotency_key: expect.stringMatching(/^memory-edit-/),
      }),
    );
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent(/Someone else wrote this note/);
    expect(within(alert).getByText(/is owned by/)).not.toBeVisible();
  });
});
