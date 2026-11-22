import { ThemeToggle } from "./ThemeToggle";
import { UiShowcase } from "./UiShowcase";

/**
 * `/ui/?showcase=primitives`: every design-system primitive on one page, with
 * the theme switch, for the primitives' axe and keyboard specs
 * (`tests/ui.a11y.spec.ts`). No hub reads, no sign-in.
 */
export function ShowcasePage() {
  return (
    <main id="main-content" className="min-h-dvh bg-[var(--ua-bg)] text-[var(--ua-fg)]">
      <div className="mx-auto max-w-6xl px-8 pt-6">
        <ThemeToggle />
      </div>
      <UiShowcase />
    </main>
  );
}

export default ShowcasePage;
