import type { NavigateFunction } from "react-router";

/** One entry in the command palette (spec §7: areas export `commands`). */
export type PaletteCommand = {
  id: string;
  label: string;
  /** Heading the entry is listed under: "Go to", "Create", "Work". */
  group?: string;
  /** Extra words that should find this entry. */
  keywords?: readonly string[];
  /** App path to open. */
  to?: string;
  /** Or an action to run. */
  run?: (ctx: { navigate: NavigateFunction }) => void;
  /** Shortcut hint, e.g. "g w". */
  shortcut?: string;
};

export function matchesCommand(command: PaletteCommand, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [command.label, command.group ?? "", ...(command.keywords ?? []), command.to ?? ""].join(" ").toLowerCase();
  return words.every((w) => haystack.includes(w));
}

/** Rank for sorting: the label starting with the query beats a label match, which beats a keyword match. */
export function commandScore(command: PaletteCommand, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const label = command.label.toLowerCase();
  if (label === q) return 3;
  if (label.startsWith(q)) return 2;
  if (label.includes(q)) return 1;
  return 0;
}
