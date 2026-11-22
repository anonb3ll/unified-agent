/**
 * `/work/canvas`: the Excalidraw planning canvas, retained from the classic
 * pane. The editor host is the classic one, imported unchanged; loading and
 * saving are rebuilt here. The board is in the URL (`?board=`), the save
 * goes through `useHubWrite` with `PUT`, and discarding unsaved changes asks
 * through `ConfirmDialog` (no `window.confirm`).
 */
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type RefObject } from "react";
import type { CanvasApi } from "../../lib/canvas/ExcalidrawHost";
import { normalizeBoardId, sceneFromElements, type GraphCanvasPayload, type GraphCanvasScene } from "../../lib/canvas/types";
import { useCanvas } from "../../lib/canvas/useCanvas";
import { Button, ConfirmDialog, Dialog, Field, Input, PageHeader, Skeleton } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { useUrlParam } from "../lib/urlState";
import { WRITE_PATHS } from "./api";
import { ago } from "./model";
import { WorkTabs, WriteFeedback } from "./shared";

const ExcalidrawHost = lazy(() => import("../../lib/canvas/ExcalidrawHost"));

/**
 * Excalidraw renders its main-menu trigger as an icon button with no name
 * (axe `button-name`). Name it from outside, without changing the editor.
 */
function useNameEditorControls(root: RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const label = () => {
      for (const b of el.querySelectorAll<HTMLButtonElement>(".main-menu-trigger:not([aria-label])")) {
        b.setAttribute("aria-label", "Canvas menu");
        b.setAttribute("title", "Canvas menu");
      }
    };
    label();
    const observer = new MutationObserver(label);
    observer.observe(el, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [root]);
}

export function CanvasPage() {
  const [board, setBoard] = useUrlParam("board", "default");
  const [input, setInput] = useState(board);
  const [boardError, setBoardError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [confirmLoad, setConfirmLoad] = useState<string | null>(null);
  /** The board whose drawing is in the editor; Save always writes there. */
  const [editorBoard, setEditorBoard] = useState<string | null>(null);
  // Back, Forward or a link changed the board while the editor holds unsaved
  // work for another one: nothing loads or saves until the person decides.
  const blocked = dirty && editorBoard !== null && board !== editorBoard;
  const apiRef = useRef<CanvasApi | null>(null);
  const editorRoot = useRef<HTMLDivElement>(null);
  useNameEditorControls(editorRoot);
  const applied = useRef<string | null>(null);
  const suppress = useRef(false);
  const query = useCanvas(board);
  const [editorShown, setEditorShown] = useState(false);
  if (!editorShown && query.data) setEditorShown(true);
  const save = useHubWrite<GraphCanvasScene, { board_id: string; updated_at: string; updated_by: string }>({
    path: WRITE_PATHS.canvas(editorBoard ?? board),
    method: "PUT",
    invalidates: [["graph-canvas", editorBoard ?? board]],
    label: "Saving the canvas",
    successMessage: (d) => `Board "${d.board_id}" saved. Agents can read it now.`,
    resumable: false,
  });

  const apply = useCallback((payload: GraphCanvasPayload) => {
    const api = apiRef.current;
    if (!api) return;
    suppress.current = true;
    api.updateElements(Array.isArray(payload.scene?.elements) ? payload.scene.elements : []);
    applied.current = `${payload.board_id}:${payload.updated_at ?? "new"}`;
    setEditorBoard(payload.board_id);
    setDirty(false);
    queueMicrotask(() => {
      suppress.current = false;
    });
  }, []);

  useEffect(() => {
    if (!query.data || !apiRef.current || dirty || query.data.board_id !== board) return;
    if (applied.current === `${query.data.board_id}:${query.data.updated_at ?? "new"}`) return;
    apply(query.data);
  }, [query.data, dirty, apply]);

  const onApiReady = useCallback(
    (api: CanvasApi) => {
      apiRef.current = api;
      if (query.data && query.data.board_id === board && !dirtyRef.current) apply(query.data);
    },
    [apply, query.data, board],
  );
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  // Every edit advances the generation; a save clears "unsaved" only if no edit came after it started.
  const generation = useRef(0);
  const onDirty = useCallback(() => {
    if (suppress.current) return;
    generation.current += 1;
    setDirty(true);
  }, []);

  const load = (target: string) => {
    applied.current = null;
    setDirty(false);
    setConfirmLoad(null);
    if (target === board) void query.refetch();
    else setBoard(target, { push: true });
  };

  return (
    <>
      <PageHeader
        title="Planning canvas"
        description="A shared drawing board. Sketch agents, tasks and workflows as labelled boxes and arrows; agents can read a saved board as suggestions."
        tabs={<WorkTabs />}
      />
      <div ref={editorRoot} className="flex flex-col gap-3">
        <form
          aria-label="Choose a board"
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const target = normalizeBoardId(input);
            if (!target) {
              setBoardError("Use lowercase letters, digits, dots, dashes or underscores, starting with a letter or digit (up to 64).");
              return;
            }
            setBoardError(null);
            if (dirty && target === board) setConfirmLoad(target);
            else if (target === board) load(target);
            else setBoard(target, { push: true });
          }}
        >
          <Field label="Board" error={boardError ?? undefined} className="min-w-48">
            <Input value={input} onChange={(e) => setInput(e.target.value)} />
          </Field>
          <Button type="submit">Open board</Button>
          <Button
            variant="primary"
            loading={save.isRunning}
            disabled={blocked || (!apiRef.current && !query.data)}
            onClick={() => {
              const api = apiRef.current;
              if (!api) return;
              const target = editorBoard ?? board;
              const savedGeneration = generation.current;
              void save.run(sceneFromElements(api.getElements()), { path: WRITE_PATHS.canvas(target) }).then((outcome) => {
                if (outcome.kind === "done") {
                  if (generation.current === savedGeneration) setDirty(false);
                  applied.current = `${outcome.data.board_id}:${outcome.data.updated_at}`;
                }
              });
            }}
          >
            Save board
          </Button>
          <span role="status" className="pb-2 text-sm text-[var(--ua-muted)]">
            {dirty ? "Unsaved changes" : ""}
          </span>
        </form>
        <WriteFeedback state={save.state} what="Saving the board" />
        <QueryRegion query={query} loadingLabel="Loading the board" errorTitle="The board could not be loaded" empty={{ title: "" }}>
          {(data) => (
            <p className="m-0 text-sm text-[var(--ua-muted)]">
              {data.exists ? `Board "${data.board_id}", last saved by ${data.updated_by ?? "someone"} ${ago(data.updated_at)}.` : `Board "${data.board_id}" is empty. Start drawing, then save.`}
            </p>
          )}
        </QueryRegion>
        {/* Stays mounted across board changes, so unsaved work survives while the next board loads. */}
        {editorShown && (
          <Suspense fallback={<Skeleton label="Loading the drawing editor" variant="block" />}>
            <ExcalidrawHost onApiReady={onApiReady} onDirty={onDirty} />
          </Suspense>
        )}
      </div>
      <Dialog
        open={blocked}
        onClose={() => editorBoard && setBoard(editorBoard)}
        title="Discard unsaved changes?"
        description={`Board "${editorBoard ?? ""}" has a drawing that is not saved. Opening board "${board}" replaces it in the editor.`}
        footer={
          <>
            <Button onClick={() => editorBoard && setBoard(editorBoard)}>{`Keep editing ${editorBoard ?? ""}`}</Button>
            <Button
              variant="danger"
              onClick={() => {
                applied.current = null;
                setDirty(false);
              }}
            >
              {`Discard and open ${board}`}
            </Button>
          </>
        }
      />
      <ConfirmDialog
        open={confirmLoad !== null}
        variant="danger"
        title="Discard unsaved changes?"
        objectName={`Board "${board}"`}
        consequence="Your drawing since the last save is lost when another board opens."
        confirmLabel="Discard and open"
        onConfirm={() => confirmLoad && load(confirmLoad)}
        onCancel={() => setConfirmLoad(null)}
      />
    </>
  );
}
