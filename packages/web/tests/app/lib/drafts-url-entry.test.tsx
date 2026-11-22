import { act, render, renderHook, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearAllDrafts, draftKey, readDraft, saveDraft, useDraft } from "../../../src/app/lib/drafts";
import { isNewAppPath } from "../../../src/app/lib/entry";
import { useUrlParams } from "../../../src/app/lib/urlState";
import { epochSetup, type EpochSetup } from "../support/epoch";

/** Drafts live in the owner epoch's storage: every test runs as "operator". */
let ep: EpochSetup;
beforeEach(() => {
  sessionStorage.clear();
  ep = epochSetup(undefined, undefined, { connect: false });
});

/** renderHook under the epoch provider. */
const renderDraftHook = ((callback: never, options: Record<string, unknown> = {}) =>
  renderHook(callback, { wrapper: ep.wrapper, ...options })) as typeof renderHook;

describe("isNewAppPath (an app route under /ui)", () => {
  it.each([
    ["/ui/", false],
    ["/ui", false],
    ["/ui/index.html", false],
    ["/", false],
    ["/ui/home", true],
    ["/ui/work/tasks/4", true],
    ["/ui/settings/integrations/slack", true],
  ])("%s -> %s", (path, expected) => expect(isNewAppPath(path)).toBe(expected));
});

describe("drafts", () => {
  it("keys by identity, operation and object, with sorted query params", () => {
    expect(draftKey({ identity: "x", operation: "task.edit", object: { id: 4, b: "2" } })).toBe(
      draftKey({ identity: "x", operation: "task.edit", object: { b: "2", id: 4 } }),
    );
    expect(draftKey({ identity: "x", operation: "task.edit", object: { note: "a", tab: "" } })).toBe(
      draftKey({ identity: "x", operation: "task.edit", object: { note: "a" } }),
    );
    expect(draftKey({ identity: null, operation: "task.create" })).not.toBe(draftKey({ identity: "anonymous", operation: "task.create" }));
  });

  it("encodes parts so different objects never collide (finding 7)", () => {
    const k = (object: Record<string, string> | string, identity = "x", operation = "memory.edit") => draftKey({ identity, operation, object });
    expect(k({ note: "a&tag=b" })).not.toBe(k({ note: "a", tag: "b" }));
    expect(k({ note: "a|b" })).not.toBe(k("note=a|b"));
    expect(draftKey({ identity: "a|b", operation: "c" })).not.toBe(draftKey({ identity: "a", operation: "b|c" }));
    expect(k({ "a=b": "c" })).not.toBe(k({ a: "b=c" }));
  });

  it("restores only on an exact key match and never across identities", () => {
    saveDraft(ep.epoch, "a|task.edit|id=1", { title: "hello" });
    expect(readDraft(ep.epoch, "a|task.edit|id=1")).toEqual({ title: "hello" });
    expect(readDraft(ep.epoch, "b|task.edit|id=1")).toBeNull();
    expect(readDraft(ep.epoch, "a|task.edit|id=12")).toBeNull();
  });

  it("useDraft saves after typing, omits secret fields, restores, and clears", () => {
    vi.useFakeTimers();
    const opts = { identity: "op", operation: "agent.add", object: { host: "cloud" }, initial: { label: "", token: "" }, omit: ["token"] as const };
    const { result, unmount } = renderDraftHook(() => useDraft({ ...opts, omit: [...opts.omit] }));
    act(() => result.current.setValue({ label: "grok", token: "secret-value" }));
    act(() => vi.advanceTimersByTime(400));
    const stored = JSON.stringify(sessionStorage);
    expect(Object.values(sessionStorage).join("")).not.toContain("secret-value");
    expect(stored).toBeDefined();
    unmount();
    const again = renderDraftHook(() => useDraft({ ...opts, omit: [...opts.omit] }));
    expect(again.result.current.value).toEqual({ label: "grok", token: "" });
    expect(again.result.current.restored).toBe(true);
    act(() => again.result.current.clear());
    expect(readDraft(ep.epoch, again.result.current.key)).toBeNull();
    vi.useRealTimers();
  });

  it("flushes the latest value when the form unmounts before the debounce (finding 6)", () => {
    vi.useFakeTimers();
    const opts = { identity: "op", operation: "task.create", initial: { title: "" } };
    const { result, unmount } = renderDraftHook(() => useDraft(opts));
    act(() => result.current.setValue({ title: "typed fast" }));
    const key = result.current.key;
    unmount();
    expect(readDraft(ep.epoch, key)).toEqual({ title: "typed fast" });
    vi.useRealTimers();
  });

  it("flushes the old object's draft when the key changes (finding 6)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderDraftHook((id: number) => useDraft({ identity: "op", operation: "task.edit", object: { id }, initial: { title: "" } }), {
      initialProps: 1,
    });
    act(() => result.current.setValue({ title: "for one" }));
    const first = result.current.key;
    rerender(2);
    expect(readDraft(ep.epoch, first)).toEqual({ title: "for one" });
    expect(result.current.value).toEqual({ title: "" });
    vi.useRealTimers();
  });

  it("clear and sign-out are not undone by the unmount flush (finding 6)", () => {
    vi.useFakeTimers();
    const opts = { identity: "op", operation: "task.create", initial: { title: "" } };
    const a = renderDraftHook(() => useDraft(opts));
    act(() => a.result.current.setValue({ title: "x" }));
    act(() => a.result.current.clear());
    a.unmount();
    expect(readDraft(ep.epoch, a.result.current.key)).toBeNull();
    const b = renderDraftHook(() => useDraft(opts));
    act(() => b.result.current.setValue({ title: "y" }));
    clearAllDrafts(ep.epoch);
    b.unmount();
    expect(readDraft(ep.epoch, b.result.current.key)).toBeNull();
    vi.useRealTimers();
  });

  it("text typed before the identity is known is kept and saved under the real key (item 3)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderDraftHook((identity: string | null | undefined) => useDraft({ identity, operation: "task.create", initial: { title: "" } }), {
      initialProps: undefined as string | null | undefined,
    });
    expect(result.current.identityPending).toBe(true);
    act(() => result.current.setValue({ title: "typed early" }));
    act(() => vi.advanceTimersByTime(400));
    // Nothing is stored under an unknown (or anonymous) key.
    expect(readDraft(ep.epoch, draftKey({ identity: null, operation: "task.create" }))).toBeNull();
    rerender("op");
    expect(result.current.value).toEqual({ title: "typed early" });
    expect(result.current.identityPending).toBe(false);
    act(() => vi.advanceTimersByTime(400));
    expect(readDraft(ep.epoch, draftKey({ identity: "op", operation: "task.create" }))).toEqual({ title: "typed early" });
    vi.useRealTimers();
  });

  it("with nothing typed, the identity's stored draft is restored when it arrives (item 3)", () => {
    saveDraft(ep.epoch, draftKey({ identity: "op", operation: "task.create" }), { title: "from before" });
    const { result, rerender } = renderDraftHook((identity: string | null | undefined) => useDraft({ identity, operation: "task.create", initial: { title: "" } }), {
      initialProps: undefined as string | null | undefined,
    });
    expect(result.current.value).toEqual({ title: "" });
    rerender("op");
    expect(result.current.value).toEqual({ title: "from before" });
    expect(result.current.restored).toBe(true);
  });

  type Props = { identity: string | null | undefined; object?: Record<string, string>; operation?: string };
  const useForm = ({ identity, object, operation = "task.edit" }: Props) => useDraft({ identity, operation, object, initial: { title: "" } });

  it("text typed for A is never adopted under B after a failed refetch (finding 4)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderDraftHook((props: Props) => useForm(props), { initialProps: { identity: "A", object: { id: "1" } } as Props });
    act(() => result.current.setValue({ title: "A's text" }));
    rerender({ identity: undefined, object: { id: "1" } });
    rerender({ identity: "B", object: { id: "1" } });
    expect(result.current.value).toEqual({ title: "" });
    act(() => vi.advanceTimersByTime(400));
    expect(readDraft(ep.epoch, draftKey({ identity: "B", operation: "task.edit", object: { id: "1" } }))).toBeNull();
    // A's draft was kept under A's own key.
    expect(readDraft(ep.epoch, draftKey({ identity: "A", operation: "task.edit", object: { id: "1" } }))).toEqual({ title: "A's text" });
    vi.useRealTimers();
  });

  it("while the identity is unknown, changing the object resets the buffer (finding 4)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderDraftHook((props: Props) => useForm(props), { initialProps: { identity: undefined, object: { id: "1" } } as Props });
    act(() => result.current.setValue({ title: "for one" }));
    rerender({ identity: undefined, object: { id: "2" } });
    expect(result.current.value).toEqual({ title: "" });
    rerender({ identity: "op", object: { id: "2" } });
    act(() => vi.advanceTimersByTime(400));
    expect(readDraft(ep.epoch, draftKey({ identity: "op", operation: "task.edit", object: { id: "2" } }))).toBeNull();
    vi.useRealTimers();
  });

  it("adoption happens only from the first unresolved state of the same form (finding 4)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderDraftHook((props: Props) => useForm(props), { initialProps: { identity: "op", object: { id: "1" } } as Props });
    rerender({ identity: undefined, object: { id: "1" } });
    act(() => result.current.setValue({ title: "typed while unknown later" }));
    rerender({ identity: "op", object: { id: "1" } });
    expect(result.current.value).toEqual({ title: "" });
    vi.useRealTimers();
  });

  it("clearAllDrafts clears mounted forms and their pending saves (finding 5)", () => {
    vi.useFakeTimers();
    const { result, unmount } = renderDraftHook(() => useForm({ identity: "op", object: { id: "1" } }));
    act(() => result.current.setValue({ title: "secret plan" }));
    act(() => clearAllDrafts(ep.epoch));
    expect(result.current.value).toEqual({ title: "" });
    act(() => vi.advanceTimersByTime(1000));
    unmount();
    expect(readDraft(ep.epoch, draftKey({ identity: "op", operation: "task.edit", object: { id: "1" } }))).toBeNull();
    vi.useRealTimers();
  });

  it("a debounce already scheduled before sign-out does not write the draft back (finding 5)", () => {
    vi.useFakeTimers();
    const key = draftKey({ identity: "op", operation: "task.edit", object: { id: "1" } });
    const { result } = renderDraftHook(() => useForm({ identity: "op", object: { id: "1" } }));
    act(() => result.current.setValue({ title: "x" }));
    // Clear storage and bump the epoch without the hook re-rendering in between.
    clearAllDrafts(ep.epoch);
    vi.advanceTimersByTime(1000);
    expect(readDraft(ep.epoch, key)).toBeNull();
    vi.useRealTimers();
  });

  it("a failed identity read stops buffering and says so (finding 7)", () => {
    const { result } = renderDraftHook(() =>
      useDraft({ identity: { status: "failed", identity: undefined, retry: () => {} }, operation: "task.create", initial: { title: "" } }),
    );
    expect(result.current.identityFailed).toBe(true);
    expect(result.current.identityPending).toBe(false);
    expect(typeof result.current.retryIdentity).toBe("function");
  });

  it.each([
    ["mounted with a failed identity", ["failed"]],
    ["loading, then failed", ["loading", "failed"]],
  ] as const)("%s: text typed is never adopted into the identity that arrives later (round 3, item 7)", (_name, before) => {
    vi.useFakeTimers();
    const state = (status: "loading" | "failed" | "ready", identity?: string) => ({ status, identity, retry: () => {} });
    const { result, rerender } = renderDraftHook(
      (who: ReturnType<typeof state>) => useDraft({ identity: who, operation: "task.create", initial: { title: "" } }),
      { initialProps: state(before[0]) },
    );
    if (before.length > 1) rerender(state(before[1]!));
    act(() => result.current.setValue({ title: "typed while failed" }));
    rerender(state("ready", "B"));
    expect(result.current.value).toEqual({ title: "" });
    act(() => vi.advanceTimersByTime(400));
    expect(readDraft(ep.epoch, draftKey({ identity: "B", operation: "task.create" }))).toBeNull();
    vi.useRealTimers();
  });

  it.each(["pagehide", "beforeunload"])("a %s within the save delay writes the latest text at once (slices)", (event) => {
    vi.useFakeTimers();
    const opts = { identity: "op", operation: "task.create", initial: { title: "" } };
    const { result } = renderDraftHook(() => useDraft(opts));
    act(() => result.current.setValue({ title: "typed then reloaded" }));
    window.dispatchEvent(new Event(event));
    expect(readDraft(ep.epoch, result.current.key)).toEqual({ title: "typed then reloaded" });
    vi.useRealTimers();
  });

  it("a pagehide after sign-out does not write the draft back (slices)", () => {
    vi.useFakeTimers();
    const opts = { identity: "op", operation: "task.create", initial: { title: "" } };
    const { result } = renderDraftHook(() => useDraft(opts));
    act(() => result.current.setValue({ title: "x" }));
    clearAllDrafts(ep.epoch);
    window.dispatchEvent(new Event("pagehide"));
    expect(readDraft(ep.epoch, result.current.key)).toBeNull();
    vi.useRealTimers();
  });

  it("clearAllDrafts drops every draft (sign-out)", () => {
    saveDraft(ep.epoch, "a|x|", { v: 1 });
    saveDraft(ep.epoch, "b|y|", { v: 2 });
    sessionStorage.setItem("unrelated", "keep");
    clearAllDrafts(ep.epoch);
    expect(readDraft(ep.epoch, "a|x|")).toBeNull();
    expect(sessionStorage.getItem("unrelated")).toBe("keep");
  });
});

describe("useUrlParams", () => {
  function Probe() {
    const { values, update, clear } = useUrlParams({ view: "list", q: "" });
    return (
      <div>
        <p>view={values.view} q={values.q}</p>
        <button onClick={() => update({ view: "board", q: "auth" })}>set</button>
        <button onClick={() => update({ view: "list" })}>default</button>
        <button onClick={clear}>clear</button>
      </div>
    );
  }

  it("reads, writes and drops defaults from the URL", () => {
    const router = createMemoryRouter([{ path: "/work", element: <Probe /> }], { initialEntries: ["/work?q=x"] });
    render(<RouterProvider router={router} />);
    expect(screen.getByText("view=list q=x")).toBeInTheDocument();
    act(() => screen.getByText("set").click());
    expect(router.state.location.search).toBe("?q=auth&view=board");
    act(() => screen.getByText("default").click());
    expect(router.state.location.search).toBe("?q=auth");
    act(() => screen.getByText("clear").click());
    expect(router.state.location.search).toBe("");
  });
});
