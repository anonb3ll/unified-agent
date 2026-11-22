import type { ReactNode } from "react";
import { act } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { vi } from "vitest";
import { WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../../src/lib/api/auth";
import type { OwnerEpoch } from "../../../src/app/lib/epoch";
import { epochSetup, type EpochSetup } from "./epoch";
import { whoamiFixture } from "./fixtures";

/**
 * The staged owner-switch harness (spec §6, "Owner epoch").
 *
 * An operation started by owner A crosses up to four boundaries. The
 * harness switches the verified owner to B at exactly one of them and
 * records every effect that lands after the switch:
 *
 * - `before-send`: the read made right before the request (the CSRF whoami
 *   read) is answered with A's identity, but B became the owner while it
 *   was in flight. For a path without such a read, B arrives as the
 *   request is about to leave (an aborted signal then means "not sent").
 * - `after-send`: the request left under A; B arrives before its answer.
 * - `after-answer`: the answer is in; B arrives while its body is read,
 *   before any callback, cache update or toast.
 * - `during-toast`: B arrives while A's toast for the result is shown.
 *
 * Two extra modes (review round 8) decouple the hub's cookie from the
 * page's cached whoami, which the stages above move together:
 *
 * - `cookieOnly`: at the boundary only the cookie becomes B's (the hub now
 *   answers whoami, and issues CSRF tokens, as B) while the page's cached
 *   whoami still says A. Writes under A's own storage scope are then A's
 *   own bookkeeping; anything carrying B's credentials is an effect.
 * - the `csrf-retry` stage: the request is refused once for CSRF, and the
 *   cookie (or the owner) changes before the retry's whoami read.
 * - the `retry-flip` stage: refused once for CSRF, the retry's whoami read
 *   still answers A, and the cookie becomes B's as the retried request
 *   arrives (the race only the hub can close). Routes see the cookie.
 *
 * Any non-GET request carrying B's CSRF token is always an effect.
 *
 * An effect is a write to Web Storage, a cache write or invalidation (in
 * A's client, in B's client under anything but B's own keys, or of the
 * root whoami), a toast visible in B's tree, a slice callback, a
 * navigation, or a write request sent. After the switch there must be
 * none.
 */
export const STAGES = ["before-send", "after-send", "after-answer", "during-toast"] as const;
export type Stage = (typeof STAGES)[number] | "csrf-retry" | "retry-flip";

export const OWNER_A = { ...whoamiFixture({ agent: "A", csrf: "csrf-a" }), org: "org-1" } as WhoamiPayload;
export const OWNER_B = { ...whoamiFixture({ agent: "B", csrf: "csrf-b" }), org: "org-1" } as WhoamiPayload;
export const CONTEXT_A = { hubOrigin: window.location.origin, organization: "org-1", identity: "A" };

type Reply = { status?: number; body?: unknown };
export type RouteRequest = {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
  /** Whose session the hub's cookie names when the request arrives. */
  cookie: WhoamiPayload;
};
export type Route = Reply | ((request: RouteRequest) => Reply);

export type HarnessOptions = {
  /** "METHOD /path" of the request whose boundaries are staged. */
  primary: string;
  /** "METHOD /path" of the read made right before it (the before-send boundary), if any. */
  before?: string;
  /** Replies; GET /api/whoami answers the current owner unless given. */
  routes?: Record<string, Route>;
  /** At the boundary, only the hub's cookie becomes B's; the page's cached whoami stays A. */
  cookieOnly?: boolean;
};

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function json(reply: Reply): Response {
  const status = reply.status ?? 200;
  const text = reply.body === undefined ? "" : JSON.stringify(reply.body);
  return new Response(status === 204 ? null : text, { status, headers: { "Content-Type": "application/json" } });
}

function abortError() {
  return new DOMException("The operation was aborted.", "AbortError");
}

export function ownerSwitchHarness(stage: Stage, options: HarnessOptions) {
  let who: WhoamiPayload = OWNER_A;
  /** The page observes B (root whoami). */
  let switched = false;
  /** The hub's cookie is B's (always true once `switched`). */
  let cookieSwitched = false;
  /** The page's epoch right after the switch (B's, when B is verified). */
  let afterSwitch: OwnerEpoch | null = null;
  let primaryStaged = false;
  let beforeStaged = false;
  let root: QueryClient | null = null;
  let current: (() => OwnerEpoch) | null = null;
  const effects: string[] = [];
  const unsubscribers: (() => void)[] = [];

  const record = (effect: string) => {
    if (switched || cookieSwitched) effects.push(effect);
  };
  /** A's own storage scope: A's bookkeeping while the page still observes A is not an effect under B. */
  const underA = (key: string) => key.includes('"A"]:');
  const recordStorage = (effect: string, key: string) => {
    if (!switched && cookieSwitched && underA(key)) return;
    record(effect);
  };

  // Web Storage: any write or removal after the switch, whoever's scope.
  const storageProto = Object.getPrototypeOf(window.sessionStorage) as Storage;
  const setItem = storageProto.setItem;
  const removeItem = storageProto.removeItem;
  const clear = storageProto.clear;
  vi.spyOn(storageProto, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
    recordStorage(`storage:set:${key}`, key);
    return setItem.call(this, key, value);
  });
  vi.spyOn(storageProto, "removeItem").mockImplementation(function (this: Storage, key: string) {
    recordStorage(`storage:remove:${key}`, key);
    return removeItem.call(this, key);
  });
  vi.spyOn(storageProto, "clear").mockImplementation(function (this: Storage) {
    record("storage:clear");
    return clear.call(this);
  });

  /** Record cache writes and invalidations in `client` after the switch, except under keys containing `ownKey`. */
  function watchClient(client: QueryClient, label: string, ownKey: string | null = null) {
    unsubscribers.push(
      client.getQueryCache().subscribe((event) => {
        if (!switched) return;
        const key = JSON.stringify(event.query.queryKey);
        if (ownKey !== null && key.includes(JSON.stringify(ownKey).slice(1, -1))) return;
        if (event.type === "added") record(`cache:${label}:added:${key}`);
        if (event.type === "updated" && (event.action.type === "success" || event.action.type === "invalidate")) {
          record(`cache:${label}:${event.action.type}:${key}`);
        }
      }),
    );
  }

  function watchRoot(client: QueryClient) {
    unsubscribers.push(
      client.getQueryCache().subscribe((event) => {
        if (!(switched || cookieSwitched) || event.type !== "updated") return;
        if (JSON.stringify(event.query.queryKey) !== JSON.stringify(WHOAMI_QUERY_KEY)) return;
        if (event.action.type === "invalidate") record("root:whoami:invalidate");
        // A reset is how a new auth generation forgets who is signed in.
        if (event.action.type === "setState") record("root:whoami:reset");
        if (event.action.type === "success") {
          const agent = (event.action.data as WhoamiPayload | null)?.agent ?? null;
          if (agent !== OWNER_B.agent) record(`root:whoami:${agent}`);
        }
      }),
    );
  }

  /** Toasts: A's toast is the during-toast boundary; any toast call made by B's epoch for A's operation is an effect. */
  function hookToasts(epoch: OwnerEpoch, owner: "A" | "B") {
    const show = epoch.toast;
    (epoch as { toast: OwnerEpoch["toast"] }).toast = (input) => {
      if (owner === "B") record(`toast:B:${input.message}`);
      const id = show(input);
      if (owner === "A") {
        toasts.push(input.message);
        if (stage === "during-toast") boundary();
      }
      return id;
    };
  }
  const toasts: ReactNode[] = [];

  /** Only the hub's cookie becomes B's: whoami and CSRF reads now answer B; the page still observes A. */
  function switchCookie() {
    if (cookieSwitched) return;
    who = OWNER_B;
    cookieSwitched = true;
  }

  /** The boundary: the cookie only (`cookieOnly`), or B as the page's verified owner. */
  function boundary() {
    if (options.cookieOnly) switchCookie();
    else switchOwner();
  }

  /** Make B the verified owner: the hub now answers B, and the page observes B. */
  function switchOwner() {
    if (switched || !root) return;
    who = OWNER_B;
    cookieSwitched = true;
    root.setQueryData(WHOAMI_QUERY_KEY, OWNER_B);
    switched = true;
    if (current) {
      const b = current();
      afterSwitch = b;
      if (b.owner?.identity === "B") {
        watchClient(b.queryClient, "B", b.ownerKey);
        hookToasts(b, "B");
      }
    }
  }

  const fetchStub = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(url, "http://localhost").pathname;
    const method = (init.method ?? "GET").toUpperCase();
    const key = `${method} ${path}`;
    const signal = init.signal ?? undefined;
    const headers = Object.fromEntries(
      Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v]),
    );
    // Whatever the stage: A's operation must never send B's credentials.
    if (method !== "GET" && headers["x-ua-csrf"] === OWNER_B.csrf) effects.push(`sent-with-B-token:${key}`);
    let body: unknown = undefined;
    if (typeof init.body === "string") body = JSON.parse(init.body);
    const route = options.routes?.[key];
    const reply = (): Reply =>
      route === undefined
        ? key === "GET /api/whoami"
          ? { body: who }
          : { status: 404, body: { error: `not mocked: ${key}` } }
        : typeof route === "function"
          ? route({ method, path, body, headers, cookie: who })
          : route;

    if (key === options.before && stage === "before-send" && !beforeStaged) {
      beforeStaged = true;
      if (options.cookieOnly) {
        // The cookie is B's by the time this read reaches the hub.
        switchCookie();
        return json(reply());
      }
      // A's answer was already on its way when B became the owner.
      const answer = { body: OWNER_A };
      switchOwner();
      await tick();
      return json(answer);
    }
    if (key === options.primary && stage === "retry-flip" && primaryStaged && !cookieSwitched) {
      // The retried request left under A; the cookie became B's on its way to the hub.
      switchCookie();
      return json(reply());
    }
    if (key !== options.primary || primaryStaged) {
      if (method !== "GET") record(`sent:${key}`);
      return json(reply());
    }
    primaryStaged = true;
    if (stage === "retry-flip") {
      // Refused once for CSRF; nothing changes yet, so the retry's read answers A.
      return json({ status: 403, body: { error: "csrf mismatch" } });
    }
    if (stage === "csrf-retry") {
      // Refused once for CSRF; B arrives before the retry's whoami read.
      if (method !== "GET") record(`sent:${key}`);
      boundary();
      await tick();
      return json({ status: 403, body: { error: "csrf mismatch" } });
    }
    if (stage === "before-send" && !options.before) {
      boundary();
      await tick();
      if (signal?.aborted) throw abortError();
    }
    if (method !== "GET") record(`sent:${key}`);
    if (stage === "after-send") {
      boundary();
      await tick();
    }
    const response = json(reply());
    if (stage === "after-answer") {
      const text = response.text.bind(response);
      const parsed = response.json.bind(response);
      Object.assign(response, {
        text: async () => {
          boundary();
          await tick();
          return text();
        },
        json: async () => {
          boundary();
          await tick();
          return parsed();
        },
      });
    }
    return response;
  });
  vi.stubGlobal("fetch", fetchStub);

  return {
    /** The boundary was reached (the owner, or with `cookieOnly` the cookie, is B's). */
    get switched() {
      return switched || cookieSwitched;
    },
    /** The effects recorded after the switch (must be empty). */
    effects,
    /** The page's epoch captured at the switch (null before it, or without a watched epoch). */
    get epochAfterSwitch() {
      return afterSwitch;
    },
    /** Messages of A's toasts (shown before the switch, or the during-toast boundary). */
    toasts,
    fetch: fetchStub,
    /** Requests made, as "METHOD /path". */
    requests: () =>
      fetchStub.mock.calls.map(([input, init]) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
        return `${((init as RequestInit | undefined)?.method ?? "GET").toUpperCase()} ${new URL(url, "http://localhost").pathname}`;
      }),
    record,
    /** A slice callback; calling it after the switch is an effect. */
    callback: <A extends unknown[], R = void>(name: string, result?: (...args: A) => R) =>
      vi.fn((...args: A): R => {
        record(`callback:${name}`);
        // Without `result` the callback returns nothing (R defaults to void).
        return result?.(...args) as R;
      }),
    /** A navigation; one after the switch is an effect. */
    navigate: vi.fn((to: string) => record(`navigate:${to}`)),
    switchOwner,
    /** Owner A's epoch over a root client whose whoami does not refetch on its own. */
    epochSetup(): EpochSetup {
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity }, mutations: { retry: false } },
      });
      const setup = epochSetup(OWNER_A, client);
      this.watch(client, () => setup.epoch);
      return setup;
    },
    /** Watch a root client (and, when given, its current epoch) that this harness switches. */
    watch(client: QueryClient, epoch?: () => OwnerEpoch) {
      root = client;
      watchRoot(client);
      if (epoch) {
        current = epoch;
        const a = epoch();
        watchClient(a.queryClient, "A");
        hookToasts(a, "A");
      }
    },
    /** Let the operation and the remount finish. */
    async settle() {
      await act(async () => {
        for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 5));
      });
    },
    dispose() {
      for (const off of unsubscribers) off();
    },
  };
}

export type OwnerSwitchHarness = ReturnType<typeof ownerSwitchHarness>;

/** The visible text of the toast region(s), "" when nothing is shown. */
export function visibleToasts(): string {
  return [...document.querySelectorAll('[aria-label="Notifications"]')].map((n) => n.textContent ?? "").join("");
}
