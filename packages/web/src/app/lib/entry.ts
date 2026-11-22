/**
 * The app is served under `/ui` (spec §3). An app route has a segment after
 * `/ui/` (for example `/ui/home`); `/ui/` itself is the landing, which opens
 * Home or rewrites a classic `?view=` link (spec §10). A segment containing a
 * dot is a file (`/ui/index.html`), never an app route.
 */
export const APP_BASENAME = "/ui";

export function isNewAppPath(pathname: string): boolean {
  const match = /^\/ui\/([^/]+)/.exec(pathname);
  if (!match) return false;
  return !match[1].includes(".");
}
