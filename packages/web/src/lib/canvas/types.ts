/**
 * Canvas-local helpers. API payload types live in hub.ts (shared with fetch/put).
 */

import type { GraphCanvasScene } from "../hub";

export type {
  GraphCanvasPayload,
  GraphCanvasSaveResult,
  GraphCanvasScene,
} from "../hub";

/** Same board-id rule as graph_canvas._BOARD_ID_RE / webui routes. */
export const BOARD_ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

export function normalizeBoardId(raw: string): string | null {
  const board = raw.trim().toLowerCase();
  if (!BOARD_ID_RE.test(board)) return null;
  return board;
}

export const SCENE_SOURCE = "unified-agent-webui";

export function sceneFromElements(elements: unknown[]): GraphCanvasScene {
  return {
    type: "excalidraw",
    version: 2,
    source: SCENE_SOURCE,
    elements,
  };
}
