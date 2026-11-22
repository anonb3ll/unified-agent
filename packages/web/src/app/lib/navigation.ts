import { useLocation, useResolvedPath } from "react-router";
import type { LinkTab } from "../../lib/ui";

export type AreaTab = { href: string; label: string; /** Match only this exact path. */ end?: boolean; count?: number | string };

/** True when `pathname` is `href` or (unless `end`) below it. */
export function isCurrentPath(pathname: string, href: string, end = false): boolean {
  const clean = href.split("?")[0].replace(/\/$/, "");
  if (pathname === clean) return true;
  return !end && pathname.startsWith(`${clean}/`);
}

/** Section tabs for `LinkTabs`, with `current` set from the location. */
export function useAreaTabs(tabs: readonly AreaTab[]): LinkTab[] {
  const { pathname } = useLocation();
  const base = useResolvedPath("/").pathname.replace(/\/$/, "");
  const appPath = base && pathname.startsWith(base) ? pathname.slice(base.length) || "/" : pathname;
  // The most specific matching tab wins, so "/work" is not current on "/work/goals".
  const matching = tabs
    .filter((t) => isCurrentPath(appPath, t.href, t.end))
    .sort((a, b) => b.href.length - a.href.length)[0];
  return tabs.map((t) => ({ href: t.href, label: t.label, count: t.count, current: t === matching }));
}
