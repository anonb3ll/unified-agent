/**
 * The sign-in generation of this tab. Sign-out (clearing stored writes and
 * drafts) starts a new one. Work that began under an older generation (a
 * write whose response arrives after sign-out began) must not store
 * anything, record a result or tell the person anything.
 */
let generation = 0;

export function authGeneration(): number {
  return generation;
}

/** Start a new generation: everything begun before it is stale. */
const listeners = new Set<() => void>();

export function bumpAuthGeneration(): void {
  generation += 1;
  for (const listener of [...listeners]) listener();
}

/** Called once each time a new generation starts (sign-out). Returns an unsubscribe. */
export function onAuthGenerationChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** True when work begun at `started` belongs to the current generation. */
export function isCurrentGeneration(started: number): boolean {
  return started === generation;
}
