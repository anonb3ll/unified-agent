import { isNewAppPath } from "./entry";
import { readSession, removeSession, writeSession } from "./storage";

/**
 * WorkOS sign-in is a full navigation, and the hub's callback lands on
 * `/ui/`, the landing. The sign-in gate remembers the URL
 * the person asked for just before leaving; `src/main.tsx` restores it once
 * when the browser comes back to `/ui/`.
 *
 * Only a same-origin path under `/ui/` with a segment (an app route) is ever
 * stored or restored, so this cannot become an open redirect.
 */
const KEY = "ua-cc2:return-to";
/** A sign-in that takes longer than this is abandoned; its path is not restored. */
export const RETURN_PATH_TTL_MS = 10 * 60_000;

/** The normalized path (pathname, search, hash) if it is a safe v2 destination, else null. */
export function safeReturnPath(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw.startsWith("/ui/") || raw.startsWith("//")) return null;
  // Browsers treat "\" as "/" and ignore some control characters in URLs.
  if (/[\\\u0000-\u001f\u007f]/.test(raw)) return null;
  let url: URL;
  try {
    url = new URL(raw, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin || !isNewAppPath(url.pathname)) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Remember where to come back to after an off-site sign-in. Unsafe paths are ignored. */
export function rememberReturnPath(path: string): void {
  const safe = safeReturnPath(path);
  if (safe) writeSession(KEY, { path: safe, at: Date.now() });
}

/** Forget it (a sign-in that completed in place). */
export function forgetReturnPath(): void {
  removeSession(KEY);
}

/**
 * Consume the remembered path. It is returned only when the page is the
 * sign-in landing (`/ui` or `/ui/`), the entry is fresh, and the path is
 * still safe; any other page load discards it.
 */
export function takeReturnPath(currentPathname: string): string | null {
  const stored = readSession<{ path?: unknown; at?: unknown } | null>(KEY, null);
  if (!stored) return null;
  removeSession(KEY);
  if (currentPathname !== "/ui" && currentPathname !== "/ui/") return null;
  if (typeof stored.at !== "number" || Date.now() - stored.at > RETURN_PATH_TTL_MS || stored.at > Date.now()) return null;
  return safeReturnPath(stored.path);
}
