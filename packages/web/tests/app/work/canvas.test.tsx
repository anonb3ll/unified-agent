import { act, fireEvent, screen, waitFor, within, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findPageHeading, renderRoute } from "../support/render";
import { CANVAS } from "./fixtures";
import { workHub } from "./hub";

vi.setConfig({ testTimeout: 40_000 });

// The real editor needs a canvas and layout jsdom does not have. This fake keeps
// the same contract as src/lib/canvas/ExcalidrawHost: get/update elements, onDirty.
vi.mock("../../../src/lib/canvas/ExcalidrawHost", async () => {
  const { useEffect, useRef } = await import("react");
  type Api = { getElements: () => unknown[]; updateElements: (e: unknown[]) => void };
  function FakeHost({ onApiReady, onDirty }: { onApiReady: (api: Api) => void; onDirty: () => void }) {
    const elements = useRef<unknown[]>([]);
    useEffect(() => {
      onApiReady({
        getElements: () => elements.current,
        updateElements: (next) => {
          elements.current = next;
        },
      });
      // Once, like the real host (it guards with readyRef).
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return (
      <div data-testid="excalidraw-host">
        <span>Fake editor</span>
        <button
          type="button"
          onClick={() => {
            elements.current = [...elements.current, { id: `shape-${elements.current.length}` }];
            onDirty();
          }}
        >
          Draw a box
        </button>
      </div>
    );
  }
  return { default: FakeHost };
});

afterEach(() => {
  // Unmount first: a form flushes its draft on unmount, which must not outlive the clear below.
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

const scene = (board: string) => ({ ...CANVAS, board_id: board, scene: { type: "excalidraw", version: 2, elements: [] } });

describe("work: canvas", () => {
  // Review round 1, blocker 4: Back between boards skipped the discard
  // confirmation, and Save wrote the previous board's drawing to the new one.
  it("a URL board change with unsaved work asks first, and Save writes to the board in the editor", async () => {
    const { router, hub } = renderRoute("/work/canvas?board=alpha", {
      hub: workHub({
        "GET /api/graph-canvas": ({ url }) => ({ body: scene(new URL(url, "http://x").searchParams.get("board") ?? "default") }),
        "PUT /api/graph-canvas": ({ url }) => ({ body: { board_id: new URL(url, "http://x").searchParams.get("board"), saved: true, updated_by: "operator", updated_at: "2026-11-10T12:00:00Z" } }),
      }),
    });
    await findPageHeading("Planning canvas");
    fireEvent.click(await screen.findByRole("button", { name: "Draw a box" }, { timeout: 15_000 }));
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();

    // What Back does: the URL changes without going through "Open board".
    await act(async () => {
      await router.navigate("/work/canvas?board=beta");
    });
    const dialog = await screen.findByRole("dialog", { name: "Discard unsaved changes?" });
    expect(dialog).toHaveTextContent('Board "alpha"');

    // Keep the drawing: the URL goes back to the board in the editor.
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep editing alpha" }));
    await waitFor(() => expect(router.state.location.search).toBe("?board=alpha"));

    fireEvent.click(screen.getByRole("button", { name: "Save board" }));
    await waitFor(() => expect(hub.calls.some((c) => c.method === "PUT")).toBe(true));
    const putUrl = hub.fn.mock.calls.map(([input, init]) => [String(input), (init as RequestInit | undefined)?.method]).find(([, m]) => m === "PUT")?.[0];
    expect(putUrl).toContain("board=alpha");
  });

  it("discarding opens the new board and saves there", async () => {
    const { router, hub } = renderRoute("/work/canvas?board=alpha", {
      hub: workHub({
        "GET /api/graph-canvas": ({ url }) => ({ body: scene(new URL(url, "http://x").searchParams.get("board") ?? "default") }),
        "PUT /api/graph-canvas": { body: { board_id: "beta", saved: true, updated_by: "operator", updated_at: "2026-11-10T12:00:00Z" } },
      }),
    });
    await findPageHeading("Planning canvas");
    fireEvent.click(await screen.findByRole("button", { name: "Draw a box" }, { timeout: 15_000 }));
    await act(async () => {
      await router.navigate("/work/canvas?board=beta");
    });
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Discard unsaved changes?" })).getByRole("button", { name: "Discard and open beta" }));
    expect(await screen.findByText(/Board "beta"/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save board" }));
    await waitFor(() => expect(hub.calls.some((c) => c.method === "PUT")).toBe(true));
    const putUrl = hub.fn.mock.calls.map(([input, init]) => [String(input), (init as RequestInit | undefined)?.method]).find(([, m]) => m === "PUT")?.[0];
    expect(putUrl).toContain("board=beta");
  });

  // Review round 2, item 17: a save that completes must not mark later edits as saved.
  it("an edit made while a save is in flight stays unsaved", async () => {
    renderRoute("/work/canvas?board=alpha", {
      hub: workHub({
        "GET /api/graph-canvas": ({ url }) => ({ body: scene(new URL(url, "http://x").searchParams.get("board") ?? "default") }),
        "PUT /api/graph-canvas": () => {
          // The person draws again while the save is on its way.
          fireEvent.click(screen.getByRole("button", { name: "Draw a box" }));
          return { body: { board_id: "alpha", saved: true, updated_by: "operator", updated_at: "2026-11-10T12:00:00Z" } };
        },
      }),
    });
    await findPageHeading("Planning canvas");
    fireEvent.click(await screen.findByRole("button", { name: "Draw a box" }, { timeout: 15_000 }));
    fireEvent.click(screen.getByRole("button", { name: "Save board" }));
    await waitFor(() => expect(screen.getAllByText(/saved/i).length).toBeGreaterThan(0));
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });
});
