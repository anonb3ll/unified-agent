type WhyGoal = { id: number; title: string };

type WhyChainPayload = {
  goal_id: number | null;
  chain: WhyGoal[];
};

export async function fetchWhyChain(
  taskId: string,
  signal?: AbortSignal,
): Promise<WhyChainPayload> {
  const response = await fetch(`/api/tasks/${taskId}/why-chain`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`why-chain HTTP ${response.status}`);
  }
  const payload = (await response.json()) as WhyChainPayload;
  return { goal_id: payload.goal_id ?? null, chain: payload.chain ?? [] };
}
