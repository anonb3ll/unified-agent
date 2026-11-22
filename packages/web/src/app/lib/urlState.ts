import { useCallback, useMemo, useRef } from "react";
import { useSearchParams } from "react-router";

export type ParamUpdate = Record<string, string | null | undefined>;

/**
 * Filters, the selected tab and the selected row live in the URL (spec §3),
 * so a reload or a shared link restores the view. Setting a value to "",
 * null or its default removes it from the URL. Updates replace the history
 * entry unless `push` is set, so filter typing does not flood back/forward.
 */
export function useUrlParams<K extends string>(defaults: Record<K, string>) {
  const [params, setParams] = useSearchParams();
  const values = useMemo(() => {
    const out = {} as Record<K, string>;
    for (const key of Object.keys(defaults) as K[]) out[key] = params.get(key) ?? defaults[key];
    return out;
    // `defaults` is usually an inline literal; compare by content.
  }, [params, JSON.stringify(defaults)]);

  // The params the next update builds on. The router's functional setter
  // passes the params of the last render, so two updates before the page
  // re-renders (choose a filter, then tick a box) would drop the first. Each
  // update records what it set here; a render with new params replaces it,
  // a render with the same (stale) params object does not.
  const pending = useRef<{ seen: URLSearchParams; next: URLSearchParams }>({ seen: params, next: params });
  if (pending.current.seen !== params) pending.current = { seen: params, next: params };

  const update = useCallback(
    (changes: Partial<Record<K, string | null>>, options: { push?: boolean } = {}) => {
      const next = new URLSearchParams(pending.current.next);
      for (const [key, value] of Object.entries(changes) as [K, string | null | undefined][]) {
        if (value === null || value === undefined || value === "" || value === defaults[key]) next.delete(key);
        else next.set(key, value);
      }
      pending.current = { ...pending.current, next };
      setParams(next, { replace: !options.push });
    },
    [setParams, JSON.stringify(defaults)],
  );

  const clear = useCallback(
    () => update(Object.fromEntries(Object.keys(defaults).map((k) => [k, null])) as Partial<Record<K, null>>),
    [update],
  );

  return { values, update, clear };
}

/** One URL parameter as `[value, setValue]`. */
export function useUrlParam(name: string, defaultValue = "") {
  const { values, update } = useUrlParams({ [name]: defaultValue } as Record<string, string>);
  const set = useCallback((value: string | null, options?: { push?: boolean }) => update({ [name]: value }, options), [update, name]);
  return [values[name], set] as const;
}

/**
 * A preset agent label from the URL (`?agent=` on Start a session, `?to=` on
 * Messages), so a page that links there for one agent opens with it chosen.
 * Anything that is not a plausible label is ignored (null).
 */
export function agentLabelParam(raw: string | null | undefined): string | null {
  const value = raw?.trim() ?? "";
  return /^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$/.test(value) ? value : null;
}

/** Where to start a session with this agent chosen. */
export function startSessionHref(agent: string): string {
  return `/sessions/new?${new URLSearchParams({ agent })}`;
}

/** Where to write a new message to this agent, with the conversations filtered to it. */
export function messageAgentHref(agent: string): string {
  return `/inbox/messages?${new URLSearchParams({ participant: agent, compose: "1", to: agent })}`;
}
