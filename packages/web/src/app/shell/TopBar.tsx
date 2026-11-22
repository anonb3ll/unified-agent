import { Plus, Search, UserRound } from "lucide-react";
import { Button, cx, focusRing, Menu, UiLink, type MenuEntry } from "../../lib/ui";
import { useTheme, type ThemeMode } from "../../lib/theme/ThemeProvider";
import { CREATE_ACTIONS } from "../routes";

export type Crumb = { label: string; title: string; href: string };

const THEMES: { id: ThemeMode; label: string }[] = [
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "high-contrast", label: "High contrast" },
];

function Breadcrumb({ crumbs }: { crumbs: Crumb[] }) {
  if (crumbs.length === 0) return <span className="text-sm font-medium">Control Center</span>;
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="m-0 flex list-none items-center gap-2 overflow-hidden p-0 text-sm font-medium">
        {crumbs.map((crumb, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={crumb.href} className={cx("flex min-w-0 items-center gap-2", !last && "max-sm:hidden")}>
              {i > 0 && (
                <span aria-hidden="true" className="text-[var(--ua-muted)]">
                  /
                </span>
              )}
              {last ? (
                <span aria-current="page" className="truncate">
                  {crumb.label}
                </span>
              ) : (
                <UiLink href={crumb.href} className={cx("truncate rounded text-[var(--ua-muted)] no-underline hover:text-[var(--ua-fg)]", focusRing)}>
                  {crumb.label}
                </UiLink>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export type TopBarProps = {
  crumbs: Crumb[];
  identity: string | null;
  onOpenPalette: () => void;
  onOpenHelp: () => void;
  onSignOut: () => void;
};

/** Breadcrumb, search (opens the palette), "New" and the account menu (spec §4). */
export function TopBar({ crumbs, identity, onOpenPalette, onOpenHelp, onSignOut }: TopBarProps) {
  const { theme, setTheme } = useTheme();
  const accountItems: MenuEntry[] = [
    { kind: "label", id: "who", label: identity ? `Signed in as ${identity}` : "Signed-in identity unknown" },
    { kind: "separator", id: "s1" },
    { kind: "label", id: "theme", label: "Theme" },
    ...THEMES.map((t) => ({ id: `theme-${t.id}`, label: t.label, checked: theme === t.id, onSelect: () => setTheme(t.id) })),
    { kind: "separator", id: "s2" },
    { id: "shortcuts", label: "Keyboard shortcuts", shortcut: "?", onSelect: onOpenHelp },
    ...(identity ? [{ id: "sign-out", label: "Sign out", onSelect: onSignOut }] : []),
  ];
  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-3 border-b border-[var(--ua-border)] bg-[var(--ua-bg)] px-5 max-md:px-3">
      <div className="min-w-0 flex-1">
        <Breadcrumb crumbs={crumbs} />
      </div>
      <Button
        onClick={onOpenPalette}
        aria-keyshortcuts="Control+K Meta+K /"
        leftIcon={<Search size={16} />}
        rightIcon={
          <kbd className="rounded border border-[var(--ua-border)] px-1.5 font-mono text-[11px] max-lg:hidden">⌘K</kbd>
        }
        className="h-9 w-[min(340px,30vw)] cursor-text !justify-start text-[var(--ua-muted)] max-lg:w-9 max-lg:!justify-center max-md:size-11 [&>span:nth-child(2)]:flex-1 [&>span:nth-child(2)]:truncate [&>span:nth-child(2)]:text-left max-lg:[&>span:nth-child(2)]:sr-only"
      >
        Search or jump to…
      </Button>
      <Menu
        label={
          <>
            <Plus aria-hidden="true" size={16} />
            <span className="max-sm:sr-only">New</span>
          </>
        }
        variant="primary"
        align="end"
        triggerClassName="max-md:min-h-11"
        items={CREATE_ACTIONS.map((a) => ({
          id: a.id,
          label: a.label,
          href: a.path,
          ...("shortcut" in a ? { shortcut: a.shortcut } : {}),
        }))}
      />
      <Menu
        label={
          <>
            <UserRound aria-hidden="true" size={16} />
            <span className="max-w-32 truncate max-lg:sr-only">{identity ?? "Account"}</span>
          </>
        }
        aria-label={identity ? `Account: ${identity}` : "Account"}
        variant="ghost"
        align="end"
        triggerClassName="max-md:min-h-11"
        items={accountItems}
      />
    </header>
  );
}
