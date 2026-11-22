import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { resetLaunchGuardCache } from "../../../src/app/sessions/launchGuard";
import { AGENTS, nodeItem, nodesPayload, sessionRow, sessionsPayload } from "./fixtures";

const NATIVE = {
  node: "desktop",
  harness: "claude",
  external_session_id: "abc",
  cwd: "/home/dev/app",
  last_activity: "2026-11-10T10:00:00Z",
  title: "Refactor auth",
  backends: ["acp:claude"],
  backend_note: null,
  resume_ready: true,
  adopted: null,
  adoptable: true,
  reason: null,
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetLaunchGuardCache();
});
afterEach(() => vi.unstubAllGlobals());

describe("/sessions list", () => {
  it("shows open sessions first with agent, task, host and state, and links each row", async () => {
    renderRoute("/sessions", { hub: { "GET /api/sessions": { body: sessionsPayload() }, "GET /api/agents": { body: AGENTS } } });
    await findPageHeading("Sessions");
    const table = await screen.findByRole("table", { name: /agent sessions/i });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual([
      "Desktop_Agent2s-live",
      "Desktop_Agent1s-wait",
      "Desktop_Agent1s-old",
    ]);
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", "/ui/sessions/s-live");
    expect(within(rows[0]).getByText("#42 Fix the login form")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText(/2 open · 3 shown/)).toBeInTheDocument();
  });

  it("sends the agent and open filters from the URL to the hub and filters host in place", async () => {
    const { hub } = renderRoute("/sessions?status=open&agent=Desktop_Agent1&host=laptop", {
      hub: { "GET /api/sessions": { body: sessionsPayload() }, "GET /api/agents": { body: AGENTS } },
    });
    await screen.findByRole("table", { name: /agent sessions/i });
    const read = hub.fn.mock.calls.map(([url]) => String(url)).find((u) => u.startsWith("/api/sessions?") && u.includes("agent="));
    expect(read).toContain("agent=Desktop_Agent1");
    expect(read).toContain("status=open");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("combobox", { name: "Host" })).toHaveValue("laptop");
  });

  it("updates the URL when a filter changes", async () => {
    const { router } = renderRoute("/sessions", { hub: { "GET /api/sessions": { body: sessionsPayload() }, "GET /api/agents": { body: AGENTS } } });
    await screen.findByRole("table", { name: /agent sessions/i });
    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "ended" } });
    expect(router.state.location.search).toBe("?status=ended");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sessions" }), { target: { value: "zzz" } });
    expect(await screen.findByText(/No session matches these filters/)).toBeInTheDocument();
  });

  it("never shows a count before the read completes", async () => {
    renderRoute("/sessions", {
      hub: {
        "GET /api/sessions": () => ({ body: sessionsPayload() }),
        "GET /api/agents": { body: AGENTS },
      },
    });
    // While loading: a skeleton and no number.
    expect(await screen.findByText(/Loading sessions/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+ open/)).toBeNull();
    expect(await screen.findByText(/2 open · 3 shown/)).toBeInTheDocument();
  });

  it("shows a plain error with Retry and keeps transport text collapsed (defect 10)", async () => {
    let fail = true;
    renderRoute("/sessions", {
      hub: {
        "GET /api/sessions": () => (fail ? { status: 500, text: "Traceback: sqlite3.OperationalError" } : { body: sessionsPayload([]) }),
        "GET /api/agents": { body: AGENTS },
      },
    });
    const error = await screen.findByRole("heading", { name: /Sessions could not be loaded/ });
    const region = error.closest("section") as HTMLElement;
    expect(within(region).getByText("The hub had a problem answering. Try again in a moment.")).toBeVisible();
    const detail = within(region).getByText(/sqlite3.OperationalError/);
    expect(detail.closest("details")).not.toHaveAttribute("open");
    fail = false;
    fireEvent.click(within(region).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "No sessions yet" })).toBeInTheDocument();
  });

  it("offers the devices, team snapshot and live activity views as tabs bound to the URL", async () => {
    const { router } = renderRoute("/sessions", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes/native-sessions": { body: { sessions: [] } },
        "GET /api/teams/snapshots": { body: { snapshots: [] } },
      },
    });
    await screen.findByRole("table", { name: /agent sessions/i });
    fireEvent.click(screen.getByRole("tab", { name: "On your devices" }));
    expect(router.state.location.search).toBe("?tab=devices");
    expect(await screen.findByRole("heading", { name: "No sessions found on devices" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Team snapshots" }));
    expect(await screen.findByRole("heading", { name: "No snapshots yet" })).toBeInTheDocument();
  });

  it("device work is reachable before (or without) a session row: open, cancel, transcript (review 6)", async () => {
    let cancelled: unknown = null;
    const queued = nodeItem({ id: "item-q", status: "queued", task_id: null, prompt_preview: "Run the tests", created_at: "2026-11-10T11:50:00Z" });
    const nodes = { ...nodesPayload(), recent_items: [queued, nodeItem({ id: "item-f", status: "failed", restartable: true })] };
    const { router } = renderRoute("/sessions?tab=work", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload([]) },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes": { body: nodes },
        "GET /api/events": { body: { events: [], seq_cursor: 0 } },
        "POST /api/action/node-session": ({ body }) => {
          cancelled = body;
          return { body: { item_id: "item-q", cancel_requested: true } };
        },
      },
    });
    const table = await screen.findByRole("table", { name: /work sent to devices/i });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    fireEvent.click(within(table).getByRole("link", { name: /item-q/ }));
    expect(router.state.location.search).toBe("?tab=work&item=item-q");
    const drawer = await screen.findByRole("dialog", { name: /Device job item-q/ });
    expect(await within(drawer).findByText("Run the tests")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "Cancel this job" }));
    const confirm = await screen.findByRole("dialog", { name: "Cancel this job?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel job" }));
    await waitFor(() => expect(cancelled).toEqual({ action: "cancel", item_id: "item-q" }));
  });

  it("continuing a restore refreshes the preview, names every seat that would run again, and goes through the launch guard (review 2: 6)", async () => {
    const restores: { body: unknown; key?: string }[] = [];
    let previews = 0;
    // Seat rows as team_snapshot.preview / restore_for return them.
    const lead = { label: "Lead", node: "desktop", backend: "acp:codex", task_id: 42, item_id: "item-1", planned: "resumed", reason: null };
    const reviewer = { label: "Reviewer", node: "desktop", backend: "acp:claude", task_id: 43, item_id: "item-2", planned: "fresh", reason: null };
    renderRoute("/sessions?tab=teams", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/teams/snapshots": { body: { snapshots: [{ snapshot_id: "snap-1", team: "Web team", taken_at: "2026-11-09T18:00:00Z", by: "operator", seats: 2 }] } },
        "GET /api/teams/snapshots/snap-1/preview": () => {
          previews += 1;
          // After the first restore, Lead's session finished, so the hub would start it again.
          return { body: { seats: previews === 1 ? [lead, reviewer] : [{ ...lead, planned: "rebuilt" }, reviewer] } };
        },
        "POST /api/teams/snapshots/snap-1/restore": ({ body, headers }) => {
          restores.push({ body, key: headers["idempotency-key"] });
          return restores.length === 1
            ? { body: { seats: [{ ...lead, outcome: "resumed", new_item_id: "item-7" }, { ...reviewer, outcome: "awaiting_approval", awaiting_approval: 55 }] } }
            : { body: { seats: [{ ...lead, planned: "rebuilt", outcome: "rebuilt", new_item_id: "item-9" }, { ...reviewer, outcome: "fresh", new_item_id: "item-8" }] } };
        },
      },
    });
    await findPageHeading("Sessions");
    fireEvent.click(await screen.findByRole("button", { name: /Preview restoring Web team/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore this team" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Restore this team?" })).getByRole("button", { name: "Restore team" }));
    const result = await screen.findByRole("table", { name: "Restore result per seat" });
    expect(within(result).getByRole("link", { name: /approval #55/ })).toHaveAttribute("href", "/ui/inbox?kind=approval&item=55");
    expect(restores[0].key).toMatch(/^launch-/);
    const before = previews;
    fireEvent.click(screen.getByRole("button", { name: "Continue the restore" }));
    const dialog = await screen.findByRole("dialog", { name: "Continue the restore?" });
    // The preview is read again for the continuation.
    await waitFor(() => expect(previews).toBeGreaterThan(before));
    expect(await within(dialog).findByText(/Lead: started in the last restore and would start again/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Reviewer: would start/)).toBeInTheDocument();
    // Running a seat again needs an explicit acknowledgement.
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Start Lead again/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(restores).toHaveLength(2));
    expect(restores[1].key).toMatch(/^launch-/);
    expect(restores[1].key).not.toBe(restores[0].key);
  }, 40_000);

  it("an adoption message edited while sending is kept and the dialog stays open (review 2: 10)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    renderRoute("/sessions?tab=devices", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes/native-sessions": { body: { sessions: [NATIVE] } },
        "POST /api/action/node-adopt": { status: 201, body: { item_id: "item-8", status: "queued" } },
      },
    });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/action/node-adopt")) await gate;
      return real(input, init);
    });
    fireEvent.click(await screen.findByRole("button", { name: /Adopt claude session on desktop/ }));
    const dialog = await screen.findByRole("dialog", { name: "Adopt this session" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Message to continue with/ }), { target: { value: "One" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Adopt and send" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Adopt and send" })).toHaveAttribute("aria-busy", "true"));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Message to continue with/ }), { target: { value: "One, and two" } });
    release();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Adopt and send" })).not.toHaveAttribute("aria-busy"));
    expect(screen.getByRole("dialog", { name: "Adopt this session" })).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: /Message to continue with/ })).toHaveValue("One, and two");
  }, 30_000);

  it("an adoption message survives closing the dialog (review 14)", async () => {
    renderRoute("/sessions?tab=devices", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes/native-sessions": {
          body: {
            sessions: [
              {
                node: "desktop",
                harness: "claude",
                external_session_id: "abc",
                cwd: "/home/dev/app",
                last_activity: "2026-11-10T10:00:00Z",
                title: "Refactor auth",
                backends: ["acp:claude"],
                backend_note: null,
                resume_ready: true,
                adopted: null,
                adoptable: true,
                reason: null,
              },
            ],
          },
        },
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Adopt claude session on desktop/ }));
    let dialog = await screen.findByRole("dialog", { name: "Adopt this session" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: /Message to continue with/ }), { target: { value: "Pick up the auth work" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: /Adopt claude session on desktop/ }));
    dialog = await screen.findByRole("dialog", { name: "Adopt this session" });
    expect(within(dialog).getByRole("textbox", { name: /Message to continue with/ })).toHaveValue("Pick up the auth work");
  });

  it("\"Start again\" on a device job goes through the launch guard (review 2: A)", async () => {
    const sent: { key?: string; body: unknown }[] = [];
    const nodes = { ...nodesPayload(), recent_items: [nodeItem({ id: "item-f", status: "failed", restartable: true })] };
    renderRoute("/sessions?tab=work&item=item-f", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes": { body: nodes },
        "GET /api/events": { body: { events: [], seq_cursor: 0 } },
        "POST /api/action/node-session": ({ body, headers }) => {
          sent.push({ key: headers["idempotency-key"], body });
          throw new TypeError("Failed to fetch");
        },
      },
    });
    const drawer = await screen.findByRole("dialog", { name: /Device job item-f/ });
    fireEvent.click(await within(drawer).findByRole("button", { name: "Start again" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Start this job again?" })).getByRole("button", { name: "Start again" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body).toEqual({ action: "restart", item_id: "item-f" });
    expect(sent[0].key).toMatch(/^launch-/);
    // The answer was lost: no second launch until it is settled on Start a session.
    await waitFor(() => expect(within(drawer).getByRole("button", { name: "Start again" })).toBeDisabled());
    expect(within(drawer).getByText(/not known whether the last launch started/)).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "Resolve it" })).toHaveAttribute("href", "/ui/sessions/new");
  });

  it("a device job links to its hub session row, resolved from the harness session id, node and item (review 2: 7)", async () => {
    // node_queue item `session_id` is the ACP (harness) session id, not the hub row id.
    const items = [nodeItem({ id: "item-1", status: "completed", session_id: "acp-1" }), nodeItem({ id: "item-z", status: "completed", session_id: "acp-unknown" })];
    const { router } = renderRoute("/sessions?tab=work&item=item-1", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload([sessionRow({ id: "s-live", external_session_id: "acp-1", meta: { node: "desktop", item_id: "item-1" } })]) },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes": { body: { ...nodesPayload(), recent_items: items } },
        "GET /api/events": { body: { events: [], seq_cursor: 0 } },
      },
    });
    const drawer = await screen.findByRole("dialog", { name: /Device job item-1/ });
    expect(await within(drawer).findByRole("link", { name: "s-live" })).toHaveAttribute("href", "/ui/sessions/s-live");
    expect(within(drawer).queryByRole("link", { name: "acp-1" })).toBeNull();
    router.navigate("/sessions?tab=work&item=item-z");
    const other = await screen.findByRole("dialog", { name: /Device job item-z/ });
    expect(await within(other).findByText(/No hub session is registered for this job yet/)).toBeInTheDocument();
    expect(within(other).queryByRole("link", { name: "acp-unknown" })).toBeNull();
  });

  it("device work refreshes on node events and on reconnect, and says when the hub listed only its latest 25 (review 2: 8)", async () => {
    // A stand-in EventSource the shared stream opens; the test sends frames through it.
    const sources: FakeEventSource[] = [];
    class FakeEventSource extends EventTarget {
      url: string;
      readyState = 1;
      constructor(url: string) {
        super();
        this.url = url;
        sources.push(this);
      }
      close() {
        this.readyState = 2;
      }
    }
    vi.stubGlobal("EventSource", FakeEventSource);
    let reads = 0;
    const many = Array.from({ length: 25 }, (_, i) => nodeItem({ id: `item-${i}`, status: "completed" }));
    renderRoute("/sessions?tab=work", {
      hub: {
        "GET /api/sessions": { body: sessionsPayload() },
        "GET /api/agents": { body: AGENTS },
        "GET /api/nodes": () => {
          reads += 1;
          return { body: { ...nodesPayload(), recent_items: many } };
        },
      },
    });
    await screen.findByRole("table", { name: /work sent to devices/i });
    expect(screen.getByText(/The hub lists only its latest 25 jobs/)).toBeInTheDocument();
    await waitFor(() => expect(sources.length).toBeGreaterThan(0));
    const before = reads;
    const frame = { seq: 1, ulid: "01NODE", channel: "agent.node", originator: "desktop", created_at: new Date().toISOString(), payload: { kind: "claimed", item_id: "item-0" } };
    sources[0].dispatchEvent(new MessageEvent("message", { data: JSON.stringify(frame) }));
    await waitFor(() => expect(reads).toBeGreaterThan(before));
    // The stream drops and reopens: missed events are caught up by a fresh read.
    const beforeReconnect = reads;
    sources[0].dispatchEvent(new Event("open"));
    sources[0].dispatchEvent(new Event("error"));
    sources[0].dispatchEvent(new Event("open"));
    await waitFor(() => expect(reads).toBeGreaterThan(beforeReconnect), { timeout: 8_000 });
  }, 30_000);
});
