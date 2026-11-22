import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  fetchGraphCanvas,
  graphCanvasQueryKey,
  putGraphCanvas,
  type GraphCanvasScene,
} from "../hub";
import { useStreamInvalidation } from "../polling";

/**
 * Graph-canvas board via TanStack Query + SSE invalidation on /api/events/stream.
 * Any message event refreshes the active board query (same pattern as Approvals).
 */
export function useCanvas(boardId: string) {
  const queryClient = useQueryClient();
  const queryKey = graphCanvasQueryKey(boardId);

  useStreamInvalidation(() => {
    void queryClient.invalidateQueries({ queryKey });
  });

  return useQuery({
    queryKey,
    queryFn: ({ signal }) => fetchGraphCanvas(boardId, signal),
  });
}

export function useCanvasSave() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      boardId,
      scene,
    }: {
      boardId: string;
      scene: GraphCanvasScene;
    }) => putGraphCanvas(boardId, scene),
    onSuccess: (_data, vars) => {
      void queryClient.invalidateQueries({
        queryKey: graphCanvasQueryKey(vars.boardId),
      });
    },
  });
}
