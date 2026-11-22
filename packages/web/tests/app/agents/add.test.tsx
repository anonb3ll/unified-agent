import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WHOAMI_QUERY_KEY } from "../../../src/lib/api/auth";
import { clearParkedWrites, listParkedWrites } from "../../../src/app/lib/parkedWrites";
import { fixtureEpoch } from "../support/epoch";
import type { MockHandler } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { findPageHeading, renderRoute } from "../support/render";
import { connectRetryDelay } from "../../../src/app/agents/connectWait";
import { ADD_RESULT, agentsHub, BEARER, OPERATOR_ONLY, parked, ROSTER } from "./fixtures";

vi.setConfig({ testTimeout: 40_000 });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
  clearParkedWrites(fixtureEpoch());
});

const ADD = "/api/action/agent/add";
const adds = <T extends { method: string; path: string }>(calls: T[]) => calls.filter((c) => c.method === "POST" && c.path === ADD);
const DETAIL_NEW = "GET /api/agents/Laptop_New";
const detailReads = <T extends { method: string; path: string }>(calls: T[]) =>
  calls.filter((c) => c.method === "GET" && c.path === "/api/agents/Laptop_New");

/** The hub's answer to `?wait_for=connected`: the never-connected detail plus `wait`. */
function waitAnswer(satisfied: boolean) {
  return {
    agent: { label: "Laptop_New", status: satisfied ? "online" : "never_connected", open_sessions: 0, active_tasks: 0 },
    connection: satisfied ? "connected" : "never_connected",
    sessions: [],
    activity: [],
    wait: { for: "connected", since: ADD_RESULT.issued_at, timeout_s: 25, satisfied, waited_s: satisfied ? 0.4 : 25 },
  };
}

/** The abort signal of the newest read of the new agent's detail. */
function heldSignal(fn: { mock: { calls: unknown[][] } }): AbortSignal | undefined {
  const reads = fn.mock.calls.filter(([input]) => String(input).startsWith("/api/agents/Laptop_New"));
  return (reads.at(-1)?.[1] as RequestInit | undefined)?.signal ?? undefined;
}

/** Walk the three steps by keyboard-equivalent events and press "Add the agent". */
async function fillAndSubmit(label = "Laptop_New") {
  await findPageHeading("Add an agent");
  fireEvent.click(await screen.findByLabelText(/Another machine/));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.change(await screen.findByLabelText(/^Label/), { target: { value: label } });
  fireEvent.change(screen.getByLabelText(/^Office/), { target: { value: "platform" } });
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  const submit = await screen.findByRole("button", { name: "Add the agent" });
  fireEvent.click(submit);
}

/** Every place a bearer must never be once the dialog is closed. */
function bearerAnywhere(result: ReturnType<typeof renderRoute>): string[] {
  const found: string[] = [];
  if (document.body.innerHTML.includes(BEARER)) found.push("DOM");
  for (const area of [sessionStorage, localStorage]) {
    for (let i = 0; i < area.length; i += 1) {
      const key = area.key(i)!;
      if ((area.getItem(key) ?? "").includes(BEARER)) found.push(`storage:${key}`);
    }
  }
  for (const client of [result.queryClient, result.appClient()]) {
    for (const query of client.getQueryCache().getAll()) {
      if (JSON.stringify(query.state.data ?? null).includes(BEARER)) found.push(`cache:${JSON.stringify(query.queryKey)}`);
    }
    for (const mutation of client.getMutationCache().getAll()) {
      if (JSON.stringify(mutation.state.data ?? null).includes(BEARER)) found.push("mutation cache");
    }
  }
  if (JSON.stringify(listParkedWrites(result.epoch())).includes(BEARER)) found.push("parked writes");
  return found;
}

describe("agents: add an agent", () => {
  it("walks three steps, sends the validated body, and shows the bearer once in a copy-once dialog", async () => {
    const result = renderRoute("/agents/add", { hub: agentsHub({ [`POST ${ADD}`]: { body: ADD_RESULT } }) });
    await fillAndSubmit();
    const dialog = await screen.findByRole("dialog", { name: "Laptop_New's token" });
    expect(within(dialog).getByText(BEARER)).toBeInTheDocument();
    expect(within(dialog).getByText(/only time the token is shown/)).toBeInTheDocument();
    expect(within(dialog).getByText(/harness configuration/)).toBeInTheDocument();
    // The paste-able command never contains the token itself.
    const snippet = within(dialog).getByText(/Paste this on the agent's host/).closest("figure");
    expect(snippet?.textContent).not.toContain(BEARER);
    expect(adds(result.hub.calls)).toHaveLength(1);
    expect(adds(result.hub.calls)[0].body).toEqual({ label: "Laptop_New", office: "platform", kind: "local" });
    // While shown, the token is still only in the DOM: never stored or cached.
    expect(bearerAnywhere(result)).toEqual(["DOM"]);

    fireEvent.click(within(dialog).getByRole("button", { name: "I have copied it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(bearerAnywhere(result)).toEqual([]);
    expect(screen.getByRole("heading", { name: "Laptop_New was added" })).toBeInTheDocument();
    expect(screen.getByText(/shown once and is no longer available/)).toBeInTheDocument();
    expect(screen.getByText(/Waiting for Laptop_New to connect to the hub/)).toBeInTheDocument();
    // The draft was cleared: it held exactly what was submitted.
    expect(Object.keys(sessionStorage).filter((k) => k.includes("agent.add"))).toEqual([]);
  });

  it("the live check asks the hub to hold the read and turns green on the first satisfied answer", async () => {
    const asked: URL[] = [];
    const { hub } = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: { body: ADD_RESULT },
        [DETAIL_NEW]: ({ url }) => {
          asked.push(new URL(url, "http://localhost"));
          return { body: waitAnswer(true) };
        },
      }),
    });
    await fillAndSubmit();
    fireEvent.click(await screen.findByRole("button", { name: "I have copied it" }));
    expect(await screen.findByText(/has connected to the hub/)).toBeInTheDocument();
    expect(asked).toHaveLength(1);
    expect(Object.fromEntries(asked[0].searchParams)).toEqual({ wait_for: "connected", timeout: "25", since: ADD_RESULT.issued_at });
    // Satisfied: nothing more is asked.
    await new Promise((r) => setTimeout(r, 1_200));
    expect(detailReads(hub.calls)).toHaveLength(1);
  });

  it("a timed-out wait is asked again; the next satisfied answer turns the check green", async () => {
    let answers = 0;
    const { hub } = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: { body: ADD_RESULT },
        [DETAIL_NEW]: () => {
          answers += 1;
          return { body: waitAnswer(answers >= 2) };
        },
      }),
    });
    await fillAndSubmit();
    fireEvent.click(await screen.findByRole("button", { name: "I have copied it" }));
    expect(await screen.findByText(/Waiting for Laptop_New to connect to the hub/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/has connected to the hub/)).toBeInTheDocument(), { timeout: 4_000 });
    expect(detailReads(hub.calls)).toHaveLength(2);
  });

  it("a 429 backs off with jitter and asks again, still showing Waiting", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const at: number[] = [];
    renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: { body: ADD_RESULT },
        [DETAIL_NEW]: () => {
          at.push(Date.now());
          return at.length === 1
            ? { status: 429, body: { error: "too many agent reads are waiting; retry shortly", code: "rate_limited" } }
            : { body: waitAnswer(true) };
        },
      }),
    });
    await fillAndSubmit();
    fireEvent.click(await screen.findByRole("button", { name: "I have copied it" }));
    await waitFor(() => expect(at).toHaveLength(1));
    // Busy, not broken: the page keeps saying it is waiting.
    expect(screen.getByText(/Waiting for Laptop_New to connect to the hub/)).toBeInTheDocument();
    expect(screen.queryByText(/could not be asked/)).toBeNull();
    await waitFor(() => expect(screen.getByText(/has connected to the hub/)).toBeInTheDocument(), { timeout: 4_000 });
    expect(at).toHaveLength(2);
    // Math.random() = 0 is the bottom of the first window: half of one second.
    expect(at[1] - at[0]).toBeGreaterThanOrEqual(450);
  });

  it("the retry delay doubles per failure, is capped, and jitters inside the upper half of its window", () => {
    expect([1, 2, 3].map((n) => connectRetryDelay(n, () => 0))).toEqual([500, 1_000, 2_000]);
    expect([1, 2, 3].map((n) => connectRetryDelay(n, () => 1))).toEqual([1_000, 2_000, 4_000]);
    expect(connectRetryDelay(20, () => 1)).toBe(30_000);
    expect(connectRetryDelay(20, () => 0)).toBe(15_000);
  });

  it("leaving the page aborts the held read and asks nothing more", async () => {
    const result = renderRoute("/agents/add", {
      hub: agentsHub({ [`POST ${ADD}`]: { body: ADD_RESULT }, [DETAIL_NEW]: () => new Promise(() => {}) }),
    });
    await fillAndSubmit();
    fireEvent.click(await screen.findByRole("button", { name: "I have copied it" }));
    await waitFor(() => expect(detailReads(result.hub.calls)).toHaveLength(1));
    const held = heldSignal(result.hub.fn);
    expect(held?.aborted).toBe(false);
    cleanup();
    expect(held?.aborted).toBe(true);
    await new Promise((r) => setTimeout(r, 1_500));
    expect(detailReads(result.hub.calls)).toHaveLength(1);
  });

  it("an owner change aborts the held read", async () => {
    const result = renderRoute("/agents/add", {
      hub: agentsHub({ [`POST ${ADD}`]: { body: ADD_RESULT }, [DETAIL_NEW]: () => new Promise(() => {}) }),
    });
    await fillAndSubmit();
    fireEvent.click(await screen.findByRole("button", { name: "I have copied it" }));
    await waitFor(() => expect(detailReads(result.hub.calls)).toHaveLength(1));
    const held = heldSignal(result.hub.fn);
    await act(async () => {
      result.queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "Mac_Hermes" }));
    });
    await waitFor(() => expect(held?.aborted).toBe(true));
    await new Promise((r) => setTimeout(r, 1_500));
    expect(detailReads(result.hub.calls)).toHaveLength(1);
  });

  it("drops the bearer when the owner changes while the dialog is open", async () => {
    const result = renderRoute("/agents/add", { hub: agentsHub({ [`POST ${ADD}`]: { body: ADD_RESULT } }) });
    await fillAndSubmit();
    await screen.findByRole("dialog", { name: "Laptop_New's token" });
    await act(async () => {
      result.queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "Mac_Hermes" }));
    });
    await waitFor(() => expect(bearerAnywhere(result)).toEqual([]));
  });

  it("an owner switch while the add is in flight never shows or stores the bearer", async () => {
    let release: (() => void) | null = null;
    const result = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: () =>
          new Promise((resolve) => {
            release = () => resolve({ body: ADD_RESULT });
          }),
      }),
    });
    await fillAndSubmit();
    await waitFor(() => expect(release).not.toBeNull());
    await act(async () => {
      result.queryClient.setQueryData(WHOAMI_QUERY_KEY, whoamiFixture({ agent: "Mac_Hermes" }));
    });
    await act(async () => release?.());
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(bearerAnywhere(result)).toEqual([]);
    expect(screen.queryByText(/Laptop_New was added/)).toBeNull();
  });

  it("records the attempt before the send; a lost answer is explained on the next visit", async () => {
    let seenDraft = "";
    const result = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: () => {
          // At the moment the request leaves, the attempt is already stored.
          seenDraft = Object.entries(sessionStorage).find(([k]) => k.includes("agent.add"))?.[1] ?? "";
          return { status: 200, text: '{"agent": "Laptop_New", "tok' };
        },
      }),
    });
    await fillAndSubmit();
    expect(await screen.findByText(/may have been made/)).toBeInTheDocument();
    expect(seenDraft).toContain('"attempt":{"label":"Laptop_New"');
    expect(bearerAnywhere(result)).toEqual([]);
    cleanup();
    renderRoute("/agents/add", { hub: agentsHub() });
    await findPageHeading("Add an agent");
    expect(await screen.findByText(/An earlier request to add/)).toBeInTheDocument();
    expect(screen.getByText(/most likely was not/)).toBeInTheDocument();
  });

  it("an answer that arrives after the form left never deletes the newer draft a later visit stored (review of b4cc9988, item 1)", async () => {
    let release: (() => void) | null = null;
    const { router } = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: () =>
          new Promise((resolve) => {
            release = () => resolve({ body: ADD_RESULT });
          }),
      }),
    });
    await fillAndSubmit();
    await waitFor(() => expect(release).not.toBeNull());
    // Leave the form while the add is in flight, come back, and edit the restored draft.
    await act(async () => {
      await router.navigate("/agents");
    });
    await findPageHeading("Agents");
    await act(async () => {
      await router.navigate("/agents/add");
    });
    await findPageHeading("Add an agent");
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    fireEvent.change(await screen.findByLabelText(/^Note/), { target: { value: "edited after coming back" } });
    await waitFor(() => expect(Object.values(sessionStorage).join("")).toContain("edited after coming back"));
    // The first form's answer arrives now: it must compare against what is stored, not its own stale text.
    await act(async () => release?.());
    await new Promise((r) => setTimeout(r, 50));
    const stored = Object.entries(sessionStorage).find(([k]) => k.includes("agent.add"))?.[1] ?? "";
    expect(stored).toContain("edited after coming back");
    expect(stored).not.toContain('"attempt":{');
  });

  it.each([
    ["already_added", /already added: it has a seat or an active token/],
    ["retired", /was removed \(retired\)/],
  ])("a 409 %s is explained and nothing is issued", async (code, text) => {
    const result = renderRoute("/agents/add", { hub: agentsHub({ [`POST ${ADD}`]: { status: 409, body: { error: "refused", code } } }) });
    await fillAndSubmit();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(bearerAnywhere(result)).toEqual([]);
    // The answer is known: the attempt marker is gone.
    expect(Object.values(sessionStorage).join("")).not.toContain('"attempt":{');
  });

  it("an unsealed store needs the explicit opt-in, which is sent and never drafted", async () => {
    const { hub } = renderRoute("/agents/add", {
      hub: agentsHub({ "GET /api/agent-roster": { body: { ...ROSTER, sealed_at_rest: false } }, [`POST ${ADD}`]: { body: ADD_RESULT } }),
    });
    await fillAndSubmit();
    expect(screen.getByText(/secret store is not encrypted/)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Add the agent" });
    expect(submit).toBeDisabled();
    expect(adds(hub.calls)).toHaveLength(0);
    fireEvent.click(screen.getByLabelText("Accept a token protected only by file permissions"));
    expect(Object.values(sessionStorage).join("")).not.toMatch(/allow_?[Pp]laintext/);
    fireEvent.click(submit);
    await screen.findByRole("dialog");
    expect(adds(hub.calls)[0].body).toMatchObject({ allow_plaintext_at_rest: true });
  });

  it("a 409 sealed_store_required shows the opt-in even when the roster said sealed", async () => {
    renderRoute("/agents/add", {
      hub: agentsHub({ [`POST ${ADD}`]: { status: 409, body: { error: "sealed store required", code: "sealed_store_required" } } }),
    });
    await fillAndSubmit();
    expect(await screen.findByText(/Nothing was issued: the hub's secret store is not encrypted/)).toBeInTheDocument();
    expect(screen.getByLabelText("Accept a token protected only by file permissions")).not.toBeChecked();
  });

  it("a parked add waits for approval, is not stored for a later run, and says to submit again", async () => {
    const result = renderRoute("/agents/add", {
      hub: agentsHub({
        [`POST ${ADD}`]: { status: 202, body: parked("agent_add", 91) },
        "GET /api/approvals/91": { body: { id: 91, tool: "agent_add", status: "pending", requested_by: "operator" } },
      }),
    });
    await fillAndSubmit();
    const notice = await screen.findByRole("region", { name: "Approval status" });
    expect(within(notice).getByText(/Nothing was issued yet/)).toBeInTheDocument();
    expect(listParkedWrites(result.epoch())).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/was added/)).toBeNull();
  });

  it("a non-operator sees why the add is disabled and the form is never offered", async () => {
    const { hub } = renderRoute("/agents/add", { hub: agentsHub({ "GET /api/agent-roster": OPERATOR_ONLY }) });
    await findPageHeading("Add an agent");
    expect(await screen.findByRole("heading", { name: "Only the operator can add agents" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add the agent" })).toBeDisabled();
    expect(screen.queryByLabelText(/^Label/)).toBeNull();
    expect(adds(hub.calls)).toHaveLength(0);
  });

  it("a 403 operator_required answer to the add is explained", async () => {
    renderRoute("/agents/add", { hub: agentsHub({ [`POST ${ADD}`]: OPERATOR_ONLY as MockHandler }) });
    await fillAndSubmit();
    expect(await screen.findByText(/Only the human operator can add or remove agents/)).toBeInTheDocument();
  });

  it("validates each step before moving on", async () => {
    renderRoute("/agents/add", { hub: agentsHub() });
    await findPageHeading("Add an agent");
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findByText("Choose where the agent runs.")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/A cloud host/));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(await screen.findByLabelText(/^Label/), { target: { value: "9bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText(/Start with a letter/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Label/), { target: { value: "Desktop_Agent1" } });
    expect(await screen.findByText(/Desktop_Agent1 is already added/)).toBeInTheDocument();
  });
});
