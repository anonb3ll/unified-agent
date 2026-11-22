import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { QueryCache, QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { HttpAuthError, WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../lib/api/auth";
import { ToastProvider, useToast, type ToastApi, type ToastInput } from "../../lib/ui/Toast";
import { authGeneration, bumpAuthGeneration, onAuthGenerationChange } from "./authGeneration";
import { keysRaw, readRaw, removeRaw, writeRaw, type StorageArea } from "./storage";
import { organizationFromWhoami, type WriteContext } from "./writeContext";

/**
 * Owner epochs (spec §6, "Owner epoch").
 *
 * An epoch is one verified owner's lifetime in this tab. Everything a page
 * does that can outlive an await (show a toast, call a slice's callback,
 * publish to a query cache, write or remove stored data, send a request)
 * goes through the epoch that was current when the operation started. When
 * the owner changes, signs out, the auth generation bumps (a 401) or another
 * tab announces a sign-in or sign-out, the epoch is revoked at once: its
 * signal aborts, its QueryClient is cancelled, cleared and unmounted, and
 * every facade below turns into a no-op. The routed tree is keyed by the
 * epoch, so a new owner gets a fresh tree with no state of the old one.
 *
 * whoami is not owner data: it lives in the root QueryClient (the one the
 * provider is rendered under), which holds nothing else for this app.
 */

/** Why a write was cancelled by the client (re-exported by `mutations.ts`). */
export type CancelReason = "signed_out" | "owner_changed" | "unverified";

export type EpochOwner = { origin: string; organization: string | null; identity: string };

export type OwnerStorage = {
  /** Parsed JSON under this owner, or `fallback` (also when revoked or without an owner). */
  read<T>(key: string, fallback: T): T;
  /** The stored text, or null. */
  readText(key: string): string | null;
  /** True only after a read-back confirms it. False when revoked, without an owner, or refused by the browser. */
  write(key: string, value: unknown): boolean;
  /** True only after a read-back confirms the key is gone. False when revoked or without an owner. */
  remove(key: string): boolean;
  /** This owner's keys starting with `prefix` (without the owner scope). */
  keys(prefix: string): string[];
};

export type OwnerLocks = {
  /**
   * Run `fn` holding a Web Lock scoped to this owner (`navigator.locks`; run
   * directly where the browser has none). Resolves undefined, without
   * running `fn`, when the epoch is revoked or has no owner, or when the
   * lock wait is aborted by revocation.
   */
  request<T>(name: string, fn: () => Promise<T> | T): Promise<T | undefined>;
};

export type OwnerEpoch = {
  readonly id: number;
  /** The verified owner, or null while signed out, signing out or unverified. */
  readonly owner: EpochOwner | null;
  /** `origin|organization|identity` as a stable string, or null. */
  readonly ownerKey: string | null;
  /** The owner as a `WriteContext` (hub origin, organization, identity), or null. */
  readonly context: WriteContext | null;
  /** The auth generation the epoch was created in. */
  readonly generation: number;
  /** Aborted when the epoch is revoked. Pass it (or a signal linked to it) to every fetch. */
  readonly signal: AbortSignal;
  /** True once revoked. Reading it also checks the root whoami and the auth generation. */
  readonly revoked: boolean;
  /** This owner's QueryClient for all app data. Dead (cleared, unmounted) once revoked. */
  readonly queryClient: QueryClient;
  /**
   * The verified owner the page observes right now: a successful whoami read
   * in the root client that names an identity. Null when it failed, is
   * loading or names nobody. Used by the per-send checks.
   */
  observed(): WriteContext | null;
  /** Runs `fn` only while the epoch is live (synchronous check); else undefined. */
  run<T>(fn: () => T): T | undefined;
  /** A callback that does nothing once the epoch is revoked. Wrap slice callbacks when an operation starts. */
  wrap<A extends unknown[], R>(callback: ((...args: A) => R) | null | undefined): (...args: A) => R | undefined;
  /** Shows a toast in this epoch's tree; dropped once revoked. */
  toast(input: ToastInput): number | undefined;
  /** sessionStorage scoped to the owner. */
  readonly storage: OwnerStorage;
  /** localStorage scoped to the owner, plus owner-scoped Web Locks (for slices that coordinate across tabs). */
  readonly sharedStorage: OwnerStorage & { locks: OwnerLocks };
};

/**
 * A sign-out in flight. It is an operation of the epoch it started in
 * (`from`), and holds a lease on exactly one transition: `from` revoked
 * and replaced by the owner-null epoch the sign-out made. The lease ends
 * the moment anything else happens after that (a sign-in in this tab or
 * another, a 401, a whoami answer naming someone other than `from`'s
 * owner): `signal` aborts, `live` turns false and `finish` does nothing.
 * So a late completion can never touch the next owner's epoch or whoami.
 */
export type SignOutHandle = {
  /** The epoch the sign-out started in (already revoked by the sign-out itself). */
  readonly from: OwnerEpoch;
  /**
   * The owner being signed out: `from`'s owner, else the verified owner the
   * page observed when the sign-out began, else null (nobody verified).
   * Every whoami read made for the request (the CSRF read and its retry's)
   * must name this owner, or nothing is sent: the hub's cookie may belong
   * to someone else by then.
   */
  readonly owner: WriteContext | null;
  /** Aborted when the lease ends. Pass it to the sign-out request. */
  readonly signal: AbortSignal;
  /** True while the lease holds (re-checks the root whoami and the auth generation). */
  readonly live: boolean;
  /**
   * After the request: clear the root whoami and read it again, only while
   * the lease holds. `cleared: false` (the request failed) only reads it again.
   */
  finish(cleared: boolean): void;
};

export type EpochHost = {
  /** The current epoch, after re-checking the root whoami and the auth generation. */
  readonly current: OwnerEpoch;
  /** The current epoch without re-checking (React's snapshot read). */
  peek(): OwnerEpoch;
  /** The root QueryClient: whoami only. */
  readonly root: QueryClient;
  subscribe(listener: () => void): () => void;
  /** Starts the eager triggers (whoami cache, auth generation, cross-tab channel). Returns a stop. */
  connect(): () => void;
  /**
   * Sign-out starts in `from`: bump the auth generation, revoke `from`,
   * tell other tabs, and return the sign-out's lease. Null (nothing done)
   * when `from` is not the current epoch: a click from a revoked tree never
   * signs out whoever is signed in now.
   */
  beginSignOut(from: OwnerEpoch): SignOutHandle | null;
  /**
   * A sign-in attempt that started in `from` finished: revoke the current
   * epoch (ending any sign-out lease), tell other tabs, and read whoami
   * again; only an answer from after this moment names the next owner.
   * Does nothing when `from` is no longer current: a confirmation that
   * arrives after someone else became the verified owner is stale.
   */
  signedIn(from: OwnerEpoch): void;
};

export const AUTH_CHANNEL = "ua-cc2-auth";
const SCOPE_PREFIX = "ua-cc2:o:";

function ownerFromWhoami(whoami: WhoamiPayload | null | undefined): EpochOwner | null {
  const identity = whoami?.agent;
  if (typeof identity !== "string" || !identity) return null;
  return {
    origin: typeof window === "undefined" ? "" : window.location.origin,
    organization: organizationFromWhoami(whoami),
    identity,
  };
}

export function ownerKeyOf(owner: EpochOwner | null): string | null {
  return owner ? JSON.stringify([owner.origin, owner.organization, owner.identity]) : null;
}

export function ownerKeyOfContext(context: WriteContext | null | undefined): string | null {
  return context?.identity
    ? ownerKeyOf({ origin: context.hubOrigin, organization: context.organization, identity: context.identity })
    : null;
}

function contextOf(owner: EpochOwner | null): WriteContext | null {
  return owner ? { hubOrigin: owner.origin, organization: owner.organization, identity: owner.identity } : null;
}

/**
 * What the root whoami says about the owner:
 * - `{ owner }` from a successful read (null owner: no identity);
 * - `{ owner: null }` from a 401;
 * - "unknown" while loading, or after any other failure (a transient
 *   failure does not end an epoch; the per-send checks still refuse).
 */
type Observation = { owner: EpochOwner | null; at: number } | "unknown";

function observe(root: QueryClient): Observation {
  const state = root.getQueryState<WhoamiPayload | null>(WHOAMI_QUERY_KEY);
  if (!state) return "unknown";
  if (state.status === "success") return { owner: ownerFromWhoami(state.data), at: state.dataUpdatedAt };
  if (state.status === "error" && state.error instanceof HttpAuthError && state.error.status === 401) {
    return { owner: null, at: state.errorUpdatedAt };
  }
  return "unknown";
}

/** The verified owner a page observes now (success with an identity), as a WriteContext. */
export function observedContext(root: QueryClient): WriteContext | null {
  const state = root.getQueryState<WhoamiPayload | null>(WHOAMI_QUERY_KEY);
  if (state?.status !== "success" || !state.data) return null;
  return contextOf(ownerFromWhoami(state.data));
}

function scoped(area: StorageArea, prefix: string, live: () => boolean): OwnerStorage {
  return {
    read<T>(key: string, fallback: T): T {
      if (!live()) return fallback;
      const raw = readRaw(area, prefix + key);
      if (raw === null) return fallback;
      try {
        return JSON.parse(raw) as T;
      } catch {
        return fallback;
      }
    },
    readText(key) {
      return live() ? readRaw(area, prefix + key) : null;
    },
    write(key, value) {
      if (!live()) return false;
      let text: string;
      try {
        text = JSON.stringify(value);
      } catch {
        return false;
      }
      return writeRaw(area, prefix + key, text);
    },
    remove(key) {
      if (!live()) return false;
      return removeRaw(area, prefix + key);
    },
    keys(keyPrefix) {
      if (!live()) return [];
      return keysRaw(area, prefix + keyPrefix).map((k) => k.slice(prefix.length));
    },
  };
}

type Mode =
  | { kind: "normal" }
  /** Sign-out in flight: the same owner answering again keeps it; anyone else revokes it. */
  | { kind: "signing-out"; from: string | null }
  /** After a 401 or a cross-tab message: only a whoami answer newer than `after` counts. */
  | { kind: "awaiting"; after: number };

let nextEpochId = 1;

/**
 * The epoch state machine, independent of React (the provider renders it;
 * tests drive it directly). `root` is the identity-only QueryClient.
 */
export function createEpochHost(root: QueryClient): EpochHost {
  const listeners = new Set<() => void>();
  let mode: Mode = { kind: "normal" };
  let current: Epoch;
  let channel: BroadcastChannel | null = null;
  let connections = 0;
  let stopTriggers: (() => void) | null = null;
  let suppressGeneration = false;

  type Epoch = OwnerEpoch & { revoke(): void; attachToast(api: ToastApi | null): void };

  function makeEpoch(owner: EpochOwner | null): Epoch {
    const id = nextEpochId++;
    const controller = new AbortController();
    const generation = authGeneration();
    const ownerKey = ownerKeyOf(owner);
    let revokedFlag = false;
    // A revoked client takes no new work. Between revocation and the remount
    // the old tree is still mounted for a moment, and a re-render there would
    // build its query again and fetch it (under whoever is signed in now).
    // After revocation `build` hands out a query from a detached cache whose
    // fetch never sends: nothing reaches this client or the network.
    const queryCache = new QueryCache();
    const deadCache = new QueryCache();
    const build = queryCache.build.bind(queryCache);
    queryCache.build = ((client, options, state) => {
      if (!revokedFlag) return build(client, options, state);
      const query = deadCache.build(client, options, state);
      query.fetch = (() => Promise.resolve(query.state.data)) as typeof query.fetch;
      return query;
    }) as typeof queryCache.build;
    const queryClient = new QueryClient({ queryCache, defaultOptions: root.getDefaultOptions() });
    let toastApi: ToastApi | null = null;
    const isLive = () => {
      if (revokedFlag) return false;
      reconcile();
      return !revokedFlag;
    };
    const withOwner = () => ownerKey !== null && isLive();
    const prefix = `${SCOPE_PREFIX}${ownerKey ?? "null"}:`;
    const shared = scoped("local", prefix, withOwner);
    const epoch: Epoch = {
      id,
      owner,
      ownerKey,
      context: contextOf(owner),
      generation,
      signal: controller.signal,
      get revoked() {
        return !isLive();
      },
      queryClient,
      observed: () => observedContext(root),
      run(fn) {
        return isLive() ? fn() : undefined;
      },
      wrap(callback) {
        return (...args) => (callback && isLive() ? callback(...args) : undefined);
      },
      toast(input) {
        return isLive() && toastApi ? toastApi.show(input) : undefined;
      },
      storage: scoped("session", prefix, withOwner),
      sharedStorage: {
        ...shared,
        locks: {
          async request(name, fn) {
            if (!withOwner()) return undefined;
            const run = async () => (isLive() ? fn() : undefined);
            const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
            if (!locks) return run();
            try {
              return (await locks.request(`${prefix}${name}`, { signal: controller.signal }, run)) as Awaited<
                ReturnType<typeof fn>
              >;
            } catch (error) {
              if (controller.signal.aborted) return undefined;
              throw error;
            }
          },
        },
      },
      revoke() {
        if (revokedFlag) return;
        revokedFlag = true;
        toastApi = null;
        controller.abort();
        void queryClient.cancelQueries();
        queryClient.clear();
      },
      attachToast(api) {
        toastApi = revokedFlag ? null : api;
      },
    };
    return epoch;
  }

  function replace(owner: EpochOwner | null, next: Mode) {
    const old = current;
    mode = next;
    current = makeEpoch(owner);
    old.revoke();
    // Revocation above is synchronous; React learns of the new epoch on the
    // next microtask, so a check made during a render never updates another
    // component while it renders.
    queueMicrotask(() => {
      for (const listener of [...listeners]) listener();
    });
  }

  /** Re-derive the owner; revoke and replace the current epoch when it no longer holds. */
  function reconcile() {
    if (current === undefined) return;
    if (current.generation !== authGeneration()) {
      awaitFresh();
      return;
    }
    const seen = observe(root);
    if (seen === "unknown") return;
    const key = ownerKeyOf(seen.owner);
    if (mode.kind === "awaiting") {
      if (seen.at < mode.after) return;
      if (key === current.ownerKey) {
        mode = { kind: "normal" };
        return;
      }
      replace(seen.owner, { kind: "normal" });
      return;
    }
    if (mode.kind === "signing-out") {
      if (key === mode.from) return;
      if (key === null) {
        mode = { kind: "normal" };
        return;
      }
      replace(seen.owner, { kind: "normal" });
      return;
    }
    if (key !== current.ownerKey) replace(seen.owner, { kind: "normal" });
  }

  let reconciling = false;
  const guardedReconcile = () => {
    if (reconciling) return;
    reconciling = true;
    try {
      reconcile();
    } finally {
      reconciling = false;
    }
  };

  /** Revoke now; only a whoami answer from after this moment can name the next owner. */
  function awaitFresh() {
    replace(null, { kind: "awaiting", after: Date.now() });
    void root.resetQueries({ queryKey: WHOAMI_QUERY_KEY });
  }

  // The first epoch: whatever the root whoami says now.
  const first = observe(root);
  current = makeEpoch(first === "unknown" ? null : first.owner);

  const host: EpochHost = {
    get current() {
      guardedReconcile();
      return current;
    },
    peek: () => current,
    root,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    connect() {
      connections += 1;
      if (connections === 1) {
        const offCache = root.getQueryCache().subscribe((event) => {
          if (JSON.stringify(event.query.queryKey) === JSON.stringify(WHOAMI_QUERY_KEY)) guardedReconcile();
        });
        const offGeneration = onAuthGenerationChange(() => {
          if (!suppressGeneration) awaitFresh();
        });
        if (typeof BroadcastChannel !== "undefined") {
          channel = new BroadcastChannel(AUTH_CHANNEL);
          channel.onmessage = () => awaitFresh();
        }
        stopTriggers = () => {
          offCache();
          offGeneration();
          channel?.close();
          channel = null;
        };
        guardedReconcile();
      }
      return () => {
        connections -= 1;
        if (connections === 0) {
          stopTriggers?.();
          stopTriggers = null;
        }
      };
    },
    beginSignOut(from) {
      if (from !== host.current || from.revoked) return null;
      const seen = observe(root);
      const fromKey = from.ownerKey ?? (seen === "unknown" ? null : ownerKeyOf(seen.owner));
      suppressGeneration = true;
      try {
        bumpAuthGeneration();
      } finally {
        suppressGeneration = false;
      }
      replace(null, { kind: "signing-out", from: fromKey });
      // The lease: the owner-null epoch this sign-out made is still current.
      // Anything after it (an owner, a 401, a sign-in, another tab) replaces
      // it, which revokes it, which ends the lease.
      const successor = current;
      const live = () => !successor.revoked && current === successor;
      const owner = from.context ?? (seen === "unknown" ? null : contextOf(seen.owner));
      announce("sign-out");
      return {
        from,
        owner,
        signal: successor.signal,
        get live() {
          return live();
        },
        finish(cleared) {
          if (!live()) return;
          if (cleared) root.setQueryData(WHOAMI_QUERY_KEY, null);
          if (mode.kind === "signing-out" && mode.from === fromKey) mode = { kind: "normal" };
          void root.invalidateQueries({ queryKey: WHOAMI_QUERY_KEY });
        },
      };
    },
    signedIn(from) {
      if (from !== host.current || from.revoked) return;
      awaitFresh();
      announce("sign-in");
    },
  };

  function announce(kind: "sign-in" | "sign-out") {
    if (typeof BroadcastChannel === "undefined") return;
    try {
      // Through this host's own channel when connected: a BroadcastChannel
      // never receives its own messages, so this tab does not revoke the
      // epoch it just made for the sign-out.
      if (channel) {
        channel.postMessage({ kind, at: Date.now() });
        return;
      }
      const out = new BroadcastChannel(AUTH_CHANNEL);
      out.postMessage({ kind, at: Date.now() });
      out.close();
    } catch {
      /* no cross-tab channel: the other tabs learn on their next whoami read */
    }
  }

  return host;
}

// ---------------------------------------------------------------- React

type EpochContextValue = { host: EpochHost; epoch: OwnerEpoch };
const EpochContext = createContext<EpochContextValue | null>(null);

/** A host supplied from above, for an `OwnerEpochProvider` rendered without one (see `EpochHostProvider`). */
const SuppliedHostContext = createContext<EpochHost | null>(null);

/**
 * Supplies the host the `OwnerEpochProvider` below uses when it is not given
 * one, so a caller outside the routed tree (a test) can reach the epoch the
 * page runs in: its storage and its QueryClient.
 */
export function EpochHostProvider({ host, children }: { host: EpochHost; children: ReactNode }) {
  return <SuppliedHostContext.Provider value={host}>{children}</SuppliedHostContext.Provider>;
}

function ToastSink({ epoch }: { epoch: OwnerEpoch }) {
  const api = useToast();
  useLayoutEffect(() => {
    (epoch as OwnerEpoch & { attachToast(api: ToastApi | null): void }).attachToast(api);
    return () => (epoch as OwnerEpoch & { attachToast(api: ToastApi | null): void }).attachToast(null);
  }, [epoch, api]);
  return null;
}

/**
 * Renders `children` under the current epoch: its own QueryClient, keyed by
 * the epoch, so a new owner remounts the whole tree, and its own toasts.
 * Render it under the root (identity-only) QueryClientProvider. Pass `host`
 * (or supply one with `EpochHostProvider`) to share one with a test.
 */
export function OwnerEpochProvider({ host: given, children }: { host?: EpochHost; children: ReactNode }) {
  const root = useQueryClient();
  const supplied = useContext(SuppliedHostContext);
  const [host] = useState(() => given ?? supplied ?? createEpochHost(root));
  useEffect(() => host.connect(), [host]);
  const epoch = useSyncExternalStore(
    host.subscribe,
    host.peek,
    host.peek,
  );
  const value = useMemo(() => ({ host, epoch }), [host, epoch]);
  return (
    <EpochContext.Provider value={value}>
      <QueryClientProvider client={epoch.queryClient} key={epoch.id}>
        <ToastProvider>
          <ToastSink epoch={epoch} />
          {children}
        </ToastProvider>
      </QueryClientProvider>
    </EpochContext.Provider>
  );
}

function useEpochContext(): EpochContextValue {
  const value = useContext(EpochContext);
  if (!value) throw new Error("This needs <OwnerEpochProvider> (the v2 shell renders one).");
  return value;
}

/** The epoch this component lives in. Capture it when an operation starts and use only it afterwards. */
export function useEpoch(): OwnerEpoch {
  return useEpochContext().epoch;
}

export function useEpochHost(): EpochHost {
  return useEpochContext().host;
}

/** The current owner's session storage (`useEpoch().storage`). */
export function useOwnerStorage(): OwnerStorage {
  return useEpoch().storage;
}

/** The current owner's shared (local) storage and Web Locks (`useEpoch().sharedStorage`). */
export function useSharedOwnerStorage(): OwnerEpoch["sharedStorage"] {
  return useEpoch().sharedStorage;
}

/** The identity-only root client (whoami). Outside a provider, the nearest client. */
export function useRootQueryClient(): QueryClient {
  const value = useContext(EpochContext);
  const nearest = useQueryClient();
  return value ? value.host.root : nearest;
}
