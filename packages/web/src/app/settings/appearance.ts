/**
 * Appearance preferences (spec §9.7) on top of the existing theme provider.
 *
 * The provider (`src/theme/ThemeProvider.tsx`) knows light, dark and high
 * contrast, and persists the choice in `localStorage["ua-web-theme"]`, as
 * the legacy theme toggle does. "System" and density are not in it yet
 * (listed under "Needs from foundation"), so they are kept here:
 *
 * - "System" stores `{ mode: "system", applied }` and sets the provider to
 *   the device's current scheme (`prefers-contrast: more` picks high
 *   contrast). It counts as chosen only while the provider still shows
 *   `applied`; a theme picked elsewhere (the account menu) wins.
 * - Density stores "comfortable" or "compact" and sets `data-density` on
 *   `<html>`; compact scales the rem-based spacing and type down slightly.
 *
 * Both choices live in the owner epoch's shared (local) storage
 * (`epoch.sharedStorage`): each owner keeps their own on this device, and
 * nothing is read or written without a verified owner.
 */
import type { ThemeMode } from "../../lib/theme/ThemeProvider";
import type { OwnerStorage } from "../lib/epoch";

export type ThemeChoice = ThemeMode | "system";
export type Density = "comfortable" | "compact";

export const THEME_PREF_KEY = "ua-cc2:theme-preference";
export const DENSITY_KEY = "ua-cc2:density";
const STYLE_ID = "ua-cc2-density-style";

function media(query: string): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

/** The theme the device asks for now. */
export function systemTheme(): ThemeMode {
  if (media("(prefers-contrast: more)")) return "high-contrast";
  return media("(prefers-color-scheme: light)") ? "light" : "dark";
}

type StoredPref = { mode: "system"; applied: ThemeMode };

function readPref(storage: OwnerStorage): StoredPref | null {
  const parsed = storage.read<Partial<StoredPref> | null>(THEME_PREF_KEY, null);
  // A malformed value reads as no preference.
  if (!parsed || typeof parsed !== "object") return null;
  if (parsed.mode === "system" && (parsed.applied === "light" || parsed.applied === "dark" || parsed.applied === "high-contrast")) {
    return { mode: "system", applied: parsed.applied };
  }
  return null;
}

/** What the person chose: "system" while the provider still shows what System applied. */
export function themeChoice(storage: OwnerStorage, current: ThemeMode): ThemeChoice {
  const pref = readPref(storage);
  return pref && pref.applied === current ? "system" : current;
}

/** Apply a choice through the provider's setter, and remember "system". */
export function chooseTheme(storage: OwnerStorage, choice: ThemeChoice, setTheme: (t: ThemeMode) => void): ThemeMode {
  if (choice === "system") {
    const applied = systemTheme();
    storage.write(THEME_PREF_KEY, { mode: "system", applied } satisfies StoredPref);
    setTheme(applied);
    return applied;
  }
  storage.remove(THEME_PREF_KEY);
  setTheme(choice);
  return choice;
}

/**
 * While "system" is chosen, follow the device when it changes. Returns the
 * unsubscribe. `current` reads the provider's theme at the moment of change.
 */
export function followSystem(storage: OwnerStorage, current: () => ThemeMode, setTheme: (t: ThemeMode) => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  const queries = ["(prefers-color-scheme: light)", "(prefers-contrast: more)"].map((q) => window.matchMedia(q));
  const onChange = () => {
    if (themeChoice(storage, current()) === "system" && systemTheme() !== current()) chooseTheme(storage, "system", setTheme);
  };
  // The device may have changed while the page was closed.
  onChange();
  for (const q of queries) q.addEventListener?.("change", onChange);
  return () => {
    for (const q of queries) q.removeEventListener?.("change", onChange);
  };
}

export function readDensity(storage: OwnerStorage): Density {
  return storage.read<unknown>(DENSITY_KEY, null) === "compact" ? "compact" : "comfortable";
}

function ensureDensityStyle(): void {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  // Tailwind spacing and type are in rem, so a smaller root size tightens the whole app.
  style.textContent = 'html[data-density="compact"]{font-size:93.75%}';
  document.head.appendChild(style);
}

export function applyDensity(density: Density): void {
  if (typeof document === "undefined") return;
  ensureDensityStyle();
  if (density === "compact") document.documentElement.dataset.density = "compact";
  else delete document.documentElement.dataset.density;
}

export function chooseDensity(storage: OwnerStorage, density: Density): void {
  if (density === "compact") storage.write(DENSITY_KEY, "compact");
  else storage.remove(DENSITY_KEY);
  applyDensity(density);
}

/** Re-apply the stored density (called when the Settings area loads). */
export function applyStoredAppearance(storage: OwnerStorage): void {
  applyDensity(readDensity(storage));
}
