import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cx } from "./cx";
import { UiLink } from "./link";

export type ToastTone = "success" | "info" | "error";

export type ToastInput = {
  message: ReactNode;
  tone?: ToastTone;
  /** A follow-up: a link ("Open approval") or a button ("Retry"). */
  action?: { label: string; href: string } | { label: string; onAction: () => void };
  /** Milliseconds before a non-error toast leaves. Errors stay until dismissed. */
  duration?: number;
};

type ToastEntry = ToastInput & { id: number };

export type ToastApi = {
  show: (toast: ToastInput) => number;
  dismiss: (id: number) => void;
};

/** Transient toasts shown at once; unresolved errors are never evicted to make room. */
const MAX_TRANSIENT = 4;

/** Drop the oldest success and info toasts beyond the limit; keep every error. */
function withRoomFor(list: ToastEntry[]): ToastEntry[] {
  const transient = list.filter((t) => (t.tone ?? "info") !== "error");
  const drop = new Set(transient.slice(0, Math.max(0, transient.length - MAX_TRANSIENT)).map((t) => t.id));
  return list.filter((t) => !drop.has(t.id));
}

const NOOP: ToastApi = { show: () => -1, dismiss: () => {} };
const ToastContext = createContext<ToastApi | null>(null);

/**
 * The page's toast API. Outside a `ToastProvider` it returns a no-op, so a
 * component that toasts still renders in isolation (tests, the design system).
 */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? NOOP;
}

const TONE: Record<ToastTone, { glyph: string; className: string }> = {
  success: { glyph: "✓", className: "border-[var(--ua-ok)]" },
  info: { glyph: "●", className: "border-[var(--ua-border-strong)]" },
  error: { glyph: "▲", className: "border-[var(--ua-danger)]" },
};

/**
 * Hosts toasts in one polite live region (`role="status"`), so each new
 * message is announced once. Success and info leave after a few seconds;
 * errors stay until the person dismisses them.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (toast: ToastInput) => {
      const id = nextId.current++;
      setToasts((list) => withRoomFor([...list, { ...toast, id }]));
      const tone = toast.tone ?? "info";
      if (tone !== "error") {
        timers.current.set(
          id,
          setTimeout(() => dismiss(id), toast.duration ?? 6000),
        );
      }
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const api = useMemo(() => ({ show, dismiss }), [show, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        role="status"
        aria-live="polite"
        aria-label="Notifications"
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2 max-md:bottom-20"
      >
        {toasts.map((toast) => {
          const tone = TONE[toast.tone ?? "info"];
          return (
            <div
              key={toast.id}
              className={cx(
                "pointer-events-auto flex items-start gap-2 rounded-md border border-l-4 bg-[var(--ua-raised)] px-3 py-2 text-sm text-[var(--ua-fg)] shadow-lg",
                tone.className,
              )}
            >
              <span aria-hidden="true">{tone.glyph}</span>
              <div className="min-w-0 flex-1">
                <div>{toast.message}</div>
                {toast.action && "href" in toast.action && (
                  <UiLink href={toast.action.href} className="text-[var(--ua-link)] underline" onClick={() => dismiss(toast.id)}>
                    {toast.action.label}
                  </UiLink>
                )}
                {toast.action && "onAction" in toast.action && (
                  <button
                    type="button"
                    onClick={() => {
                      const action = toast.action as { onAction: () => void };
                      dismiss(toast.id);
                      action.onAction();
                    }}
                    className="mt-1 cursor-pointer rounded border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] px-2 py-0.5 text-xs font-medium text-[var(--ua-fg)] hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] max-md:min-h-11"
                  >
                    {toast.action.label}
                  </button>
                )}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
                className="inline-grid size-6 shrink-0 cursor-pointer place-items-center rounded border-0 bg-transparent text-[var(--ua-fg)] hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] max-md:size-11"
              >
                <span aria-hidden="true">✕</span>
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
