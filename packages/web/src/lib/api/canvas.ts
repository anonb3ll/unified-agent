/**
 * Graph canvas boards (GET/PUT /api/graph-canvas).
 * Re-exported from `hub.ts` (task #1575).
 */
import { fetchCsrfToken, readErrorDetail } from "./http";

/** Graph-canvas scene matching GET/PUT /api/graph-canvas (task #807 / #896). */
export type GraphCanvasScene = {
  type?: string;
  version?: number;
  source?: string;
  elements?: unknown[];
  [key: string]: unknown;
};

export type GraphCanvasPayload = {
  board_id: string;
  exists: boolean;
  updated_by: string | null;
  updated_at: string | null;
  scene: GraphCanvasScene;
};

export type GraphCanvasSaveResult = {
  board_id: string;
  saved: boolean;
  updated_by: string;
  updated_at: string;
};

/** TanStack Query key for GET /api/graph-canvas?board=. */
export function graphCanvasQueryKey(boardId: string) {
  return ["graph-canvas", boardId] as const;
}

/**
 * Load a planning-canvas board via same-origin cookies.
 */
export async function fetchGraphCanvas(
  boardId: string,
  signal?: AbortSignal,
): Promise<GraphCanvasPayload> {
  const qs = `?board=${encodeURIComponent(boardId)}`;
  const response = await fetch(`/api/graph-canvas${qs}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `graph-canvas HTTP ${response.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  const payload = (await response.json()) as GraphCanvasPayload;
  return {
    board_id: payload.board_id ?? boardId,
    exists: Boolean(payload.exists),
    updated_by: payload.updated_by ?? null,
    updated_at: payload.updated_at ?? null,
    scene:
      payload.scene && typeof payload.scene === "object" ? payload.scene : {},
  };
}

/**
 * PUT /api/graph-canvas with CSRF. On 403 csrf errors, refresh once and retry.
 */
export async function putGraphCanvas(
  boardId: string,
  scene: GraphCanvasScene,
  signal?: AbortSignal,
): Promise<GraphCanvasSaveResult> {
  let csrf = await fetchCsrfToken(signal);
  const qs = `?board=${encodeURIComponent(boardId)}`;

  const attempt = async (
    token: string,
    retried: boolean,
  ): Promise<GraphCanvasSaveResult> => {
    const response = await fetch(`/api/graph-canvas${qs}`, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-ua-csrf": token,
      },
      body: JSON.stringify(scene),
      signal,
    });

    if (response.status === 403 && !retried) {
      const detail = await readErrorDetail(response);
      if (/csrf/i.test(detail)) {
        csrf = await fetchCsrfToken(signal);
        return attempt(csrf, true);
      }
    }

    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(detail || `graph-canvas PUT HTTP ${response.status}`);
    }

    const payload = (await response.json()) as GraphCanvasSaveResult;
    return {
      board_id: payload.board_id ?? boardId,
      saved: Boolean(payload.saved),
      updated_by: payload.updated_by ?? "ui_user",
      updated_at: payload.updated_at ?? "",
    };
  };

  return attempt(csrf, false);
}
