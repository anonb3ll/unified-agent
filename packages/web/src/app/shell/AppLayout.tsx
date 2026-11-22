import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Outlet, useHref, useLocation, useMatches, useNavigate } from "react-router";
import { HttpAuthError, WORKOS_SIGN_IN_PATH, type WhoamiPayload } from "../../lib/api/auth";
import { SignInView } from "../../lib/auth/SignInView";
import { LinkProvider } from "../../lib/ui";
import { clearAllDrafts } from "../lib/drafts";
import { OwnerEpochProvider, useEpoch, useEpochHost } from "../lib/epoch";
import { useWhoami } from "../lib/identity";
import { executeHubWrite } from "../lib/mutations";
import { clearParkedWrites } from "../lib/parkedWrites";
import { forgetReturnPath, rememberReturnPath } from "../lib/returnTo";
import { clearAllAttempts } from "../runs/attempt";
import { clearLaunchRecords } from "../sessions/launchGuard";
import { isRouteHandle, resolveText } from "../lib/routeHandle";
import { BottomTabBar } from "./BottomTabBar";
import { CommandPalette } from "./CommandPalette";
import { RouterLink } from "./RouterLink";
import { ShortcutHelp } from "./ShortcutHelp";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TopBar, type Crumb } from "./TopBar";
import { useAppShortcuts } from "./useAppShortcuts";

/** The sidebar's collapsed state, kept per owner (`epoch.sharedStorage`). */
const SIDEBAR_KEY = "sidebar";
export const APP_TITLE = "Control Center";

/** Breadcrumb trail from route handles, deepest last. */
function useCrumbs(): Crumb[] {
  const matches = useMatches();
  return useMemo(() => {
    const crumbs: Crumb[] = [];
    for (const match of matches) {
      if (!isRouteHandle(match.handle)) continue;
      const label = resolveText(match.handle.crumb, match.params);
      if (!label) continue;
      const title = resolveText(match.handle.title, match.params) ?? label;
      const href = match.pathname.replace(/\/$/, "") || "/";
      if (crumbs.length > 0 && crumbs[crumbs.length - 1].href === href) continue;
      crumbs.push({ label, title, href });
    }
    return crumbs;
  }, [matches]);
}

/**
 * Focus the new page's `h1` after a navigation (not on first load, so the skip
 * link stays first). Pages load lazily, so wait a few frames for the heading.
 *
 * The focus lands a frame or more after the page appears. If by then focus
 * has moved into the page itself (the person clicked a field and started
 * typing, or the page focused one of its own controls), it stays there:
 * taking it to the heading would send the rest of what they type nowhere.
 */
function useFocusOnNavigate(mainRef: React.RefObject<HTMLElement | null>) {
  const { pathname } = useLocation();
  const first = useRef(true);
  // A layout effect, so the focus at commit is read before anything (a click,
  // a key, the page's own effects) can move it.
  useLayoutEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const atCommit = document.activeElement;
    let frames = 0;
    let handle = 0;
    const tryFocus = () => {
      const main = mainRef.current;
      const now = document.activeElement;
      if (main && now instanceof HTMLElement && now !== atCommit && now !== main && main.contains(now)) return;
      const heading = main?.querySelector<HTMLElement>("h1");
      if (heading) {
        heading.focus({ preventScroll: false });
        return;
      }
      if (frames++ < 60) handle = requestAnimationFrame(tryFocus);
      else mainRef.current?.focus();
    };
    handle = requestAnimationFrame(tryFocus);
    return () => cancelAnimationFrame(handle);
  }, [pathname, mainRef]);
}

function Shell({ whoami, refreshedAt }: { whoami: WhoamiPayload | null | undefined; refreshedAt: number }) {
  const identity = whoami?.agent ?? null;
  const hubVersion = whoami?.hub_version ?? null;
  const epoch = useEpoch();
  const host = useEpochHost();
  // Per owner: the tree remounts with each epoch, so this is read once per owner.
  const [collapsed, setCollapsed] = useState(() => epoch.sharedStorage.read<string>(SIDEBAR_KEY, "expanded") === "collapsed");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const crumbs = useCrumbs();
  const navigate = useNavigate();

  useFocusOnNavigate(mainRef);

  const pageTitle = crumbs[crumbs.length - 1]?.title;
  useEffect(() => {
    document.title = pageTitle ? `${pageTitle} · ${APP_TITLE}` : APP_TITLE;
  }, [pageTitle]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      epoch.sharedStorage.write(SIDEBAR_KEY, next ? "collapsed" : "expanded");
      return next;
    });
  }, [epoch]);

  // Sign-out works even when who is signed in cannot be confirmed (an epoch
  // without an owner). It is an operation of the epoch it started in: see
  // `SignOutHandle` for the lease that ends it.
  const handleSignOut = useCallback(async () => {
    const from = epoch;
    if (from.revoked) return;
    // Dropped the moment sign-out begins, before its own request (spec §6),
    // under the epoch it started in: nothing typed or parked under this
    // identity outlives the click.
    clearAllDrafts(from);
    clearParkedWrites(from);
    // Records that hold a start or a launch whose answer was lost.
    clearAllAttempts(from);
    clearLaunchRecords(from);
    forgetReturnPath();
    // Revokes `from` (and tells other tabs). Null when `from` is no longer
    // current: a stale click never signs out whoever is signed in now.
    const signOut = host.beginSignOut(from);
    if (!signOut) return;
    const outcome = await executeHubWrite({
      path: "/api/session",
      method: "DELETE",
      signal: signOut.signal,
      // The whoami read that issues the CSRF token (and its retry's) must name
      // the owner being signed out: the cookie may be someone else's by now,
      // and their session is not this click's to end.
      ...(signOut.owner ? { sessionContext: signOut.owner } : {}),
      stillValid: () => signOut.live,
    });
    if (outcome.kind === "cancelled" || (outcome.kind === "error" && outcome.aborted)) return;
    // Not sent (or not retried) because the cookie names someone else, or
    // could not be read: cancelled. The page learns the new owner from its
    // own next whoami read (another tab's sign-in also messages this one).
    if (outcome.kind === "error" && (outcome.contextMismatch || outcome.contextUnverified)) return;
    // The hub refused because the token is not the session its cookie names
    // (the cookie changed after our last whoami read): nobody was signed
    // out. Read whoami again to learn who is; never reset it.
    if (outcome.kind === "error" && outcome.code === "csrf_session_mismatch") {
      signOut.finish(false);
      return;
    }
    // Does nothing once the lease ended (someone else is the owner now).
    signOut.finish(outcome.kind === "done");
  }, [epoch, host]);

  useAppShortcuts({
    onNavigate: (path) => navigate(path),
    onOpenPalette: () => setPaletteOpen(true),
    onOpenHelp: () => setHelpOpen(true),
    onToggleSidebar: toggleCollapsed,
  });

  return (
    <div className="min-h-dvh bg-[var(--ua-bg)] text-[var(--ua-fg)] md:flex">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <Sidebar collapsed={collapsed} onToggleCollapsed={toggleCollapsed} />
      <div className="flex min-h-dvh min-w-0 flex-1 flex-col">
        <TopBar
          crumbs={crumbs}
          identity={identity}
          onOpenPalette={() => setPaletteOpen(true)}
          onOpenHelp={() => setHelpOpen(true)}
          onSignOut={handleSignOut}
        />
        <main
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
          className="mx-auto w-full max-w-[1440px] flex-1 px-6 pb-24 pt-5 focus:outline-none max-md:px-4 md:pb-8"
        >
          <Outlet />
        </main>
        <StatusBar hubVersion={hubVersion} refreshedAt={refreshedAt} />
      </div>
      <BottomTabBar />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onOpenHelp={() => setHelpOpen(true)} />
      <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

function SignInGate() {
  const host = useEpochHost();
  // The sign-in attempt belongs to this epoch: a confirmation that arrives
  // after someone else became the verified owner (the form unmounted) is
  // stale and does nothing.
  const epoch = useEpoch();
  const location = useLocation();
  // The full URL of the requested page, basename included ("/ui/agents/hosts").
  const here = useHref({ pathname: location.pathname, search: location.search, hash: location.hash });
  useEffect(() => {
    document.title = `Sign in · ${APP_TITLE}`;
  }, []);
  // A token sign-in happens in place, so the URL never changes and the person
  // stays on the page they asked for. WorkOS sign-in leaves the site and the
  // hub brings the browser back to /ui/, so remember the page on that click.
  return (
    <div
      onClickCapture={(event) => {
        const link = (event.target as HTMLElement).closest("a");
        if (link && new URL(link.href, window.location.origin).pathname === WORKOS_SIGN_IN_PATH) {
          rememberReturnPath(here);
        }
      }}
    >
      <SignInView
        onSuccess={epoch.wrap(() => {
          forgetReturnPath();
          // Tells other tabs (they revoke their epochs) and reads whoami again.
          host.signedIn(epoch);
        })}
      />
    </div>
  );
}

/**
 * The v2 shell (spec §4): whoami in the root client, the owner epoch, the
 * sign-in gate, chrome around the routed page. Everything below
 * `OwnerEpochProvider` is keyed by the epoch: a new owner gets a new tree,
 * its own QueryClient and its own toasts.
 */
export function AppLayout() {
  const whoami = useWhoami();
  const unauthorized = whoami.isError && whoami.error instanceof HttpAuthError && whoami.error.status === 401;

  return (
    <LinkProvider component={RouterLink}>
      <OwnerEpochProvider>
        {whoami.status === "pending" ? (
          // Until whoami first answers there is no owner: mounting the page now
          // would only remount it (and refetch everything) when the owner arrives.
          <p role="status" className="sr-only">
            Checking who is signed in…
          </p>
        ) : unauthorized ? (
          <SignInGate />
        ) : (
          <Shell whoami={whoami.data} refreshedAt={whoami.dataUpdatedAt} />
        )}
      </OwnerEpochProvider>
    </LinkProvider>
  );
}
