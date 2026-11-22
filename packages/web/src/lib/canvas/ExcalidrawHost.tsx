import { useCallback, useEffect, useRef } from "react";
import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";

/** Minimal imperative surface we need from Excalidraw. */
type ExcalidrawApiLike = {
  getSceneElementsIncludingDeleted: () => unknown[];
  updateScene: (scene: { elements: unknown[] }) => void;
};

export type CanvasApi = {
  getElements: () => unknown[];
  updateElements: (elements: unknown[]) => void;
};

type ExcalidrawHostProps = {
  onApiReady: (api: CanvasApi) => void;
  onDirty: () => void;
};

/**
 * Thin host around the third-party Excalidraw editor.
 * Parent owns load/save chrome; this only exposes get/update elements.
 */
export default function ExcalidrawHost({ onApiReady, onDirty }: ExcalidrawHostProps) {
  const apiRef = useRef<ExcalidrawApiLike | null>(null);
  const readyRef = useRef(false);

  const handleApi = useCallback(
    (api: ExcalidrawApiLike) => {
      apiRef.current = api;
      if (readyRef.current) return;
      readyRef.current = true;
      onApiReady({
        getElements: () => api.getSceneElementsIncludingDeleted(),
        updateElements: (elements) => {
          api.updateScene({ elements });
        },
      });
    },
    [onApiReady],
  );

  useEffect(() => {
    return () => {
      apiRef.current = null;
      readyRef.current = false;
    };
  }, []);

  return (
    <div
      className="h-[min(70vh,560px)] min-h-[24rem] w-full overflow-hidden rounded-md border border-[var(--ua-border)]"
      data-testid="excalidraw-host"
    >
      <Excalidraw
        excalidrawAPI={handleApi as never}
        initialData={{
          appState: { viewBackgroundColor: "#ffffff" },
        }}
        onChange={() => {
          onDirty();
        }}
        UIOptions={{
          canvasActions: {
            loadScene: false,
            export: false,
            saveAsImage: false,
          },
        }}
      />
    </div>
  );
}
