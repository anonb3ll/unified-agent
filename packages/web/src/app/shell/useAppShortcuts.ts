import { useEffect, useRef } from "react";
import { DESTINATIONS } from "../routes";

/** How long `g` waits for its second key. */
const CHORD_MS = 1200;

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export type AppShortcutHandlers = {
  onNavigate: (path: string) => void;
  onOpenPalette: () => void;
  onOpenHelp: () => void;
  onToggleSidebar: () => void;
};

/** Every shortcut, for the help dialog and tests (spec §4). */
export const SHORTCUTS: readonly { keys: string; does: string }[] = [
  ...DESTINATIONS.map((d) => ({ keys: `g ${d.shortcut}`, does: `Go to ${d.label}` })),
  { keys: "c", does: "Create a task" },
  { keys: "/", does: "Search" },
  { keys: "Ctrl K or ⌘ K", does: "Search and commands" },
  { keys: "?", does: "Show keyboard shortcuts" },
  { keys: "[", does: "Collapse or expand the sidebar" },
];

/**
 * Global keys: `g` then a destination letter, `c` create task, `/` search,
 * `?` help, `[` sidebar, Ctrl/⌘K palette. Single keys never fire while the
 * person is typing, holds a modifier, or has a dialog open.
 */
export function useAppShortcuts(handlers: AppShortcutHandlers): void {
  const ref = useRef(handlers);
  ref.current = handlers;

  useEffect(() => {
    let pendingG = 0;
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
        if (document.querySelector("dialog[open]")) return;
        event.preventDefault();
        ref.current.onOpenPalette();
        return;
      }
      if (
        event.defaultPrevented ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        isTypingTarget(event.target) ||
        document.querySelector("dialog[open]")
      ) {
        pendingG = 0;
        return;
      }
      const key = event.key;
      const now = Date.now();
      const armed = pendingG !== 0 && now - pendingG <= CHORD_MS;
      pendingG = 0;
      if (armed) {
        const destination = DESTINATIONS.find((d) => d.shortcut === key.toLowerCase());
        if (destination) {
          event.preventDefault();
          ref.current.onNavigate(destination.path);
          return;
        }
      }
      switch (key) {
        case "g":
          pendingG = now;
          return;
        case "c":
          event.preventDefault();
          ref.current.onNavigate("/work/tasks/new");
          return;
        case "/":
          event.preventDefault();
          ref.current.onOpenPalette();
          return;
        case "?":
          event.preventDefault();
          ref.current.onOpenHelp();
          return;
        case "[":
          event.preventDefault();
          ref.current.onToggleSidebar();
          return;
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
