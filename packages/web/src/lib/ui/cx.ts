/** Join class names, dropping falsy parts. */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** The shared keyboard focus ring (design protocol §3): 3px accent outline, offset 2px. */
export const focusRing =
  "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)]";

/**
 * Text-entry control chrome. The boundary is ua-border-strong on ua-surface,
 * which clears the 3:1 non-text floor in every theme; phones get a 44px target.
 */
export const controlClass = cx(
  "block w-full rounded-md border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] px-2.5 py-1.5 text-sm text-[var(--ua-fg)]",
  "min-h-9 max-md:min-h-11 placeholder:text-[var(--ua-faint)]",
  "disabled:cursor-not-allowed disabled:opacity-60",
  "aria-[invalid=true]:border-[var(--ua-danger)]",
  focusRing,
);
