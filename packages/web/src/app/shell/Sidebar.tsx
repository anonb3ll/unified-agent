import { NavLink } from "react-router";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { cx, focusRing, IconButton } from "../../lib/ui";
import { DESTINATIONS } from "../routes";
import { useInboxCount } from "./InboxCount";

/**
 * The eight destinations (spec §4), icon plus label. Below 1280px, or after
 * `[`, it is an icon rail with the labels kept for assistive tech; on phones
 * the bottom tab bar replaces it.
 */
export function Sidebar({ collapsed, onToggleCollapsed }: { collapsed: boolean; onToggleCollapsed: () => void }) {
  const inbox = useInboxCount();
  const labelClass = collapsed ? "sr-only" : "max-xl:sr-only";
  return (
    <aside
      className={cx(
        "sticky top-0 hidden h-dvh shrink-0 flex-col gap-1 border-r border-[var(--ua-border)] bg-[var(--ua-surface)] p-2.5 md:flex",
        collapsed ? "w-16" : "w-16 xl:w-56",
      )}
    >
      <div className="flex items-center gap-2.5 px-1.5 pb-3 pt-1">
        <span
          aria-hidden="true"
          className="grid size-7 shrink-0 place-items-center rounded-[7px] bg-[var(--ua-accent)] font-mono text-[11px] font-semibold text-[var(--ua-accent-fg)]"
        >
          ua
        </span>
        <span className={cx("whitespace-nowrap font-semibold", labelClass)}>Control Center</span>
      </div>
      <nav aria-label="Primary">
        <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
          {DESTINATIONS.map((d) => {
            const Icon = d.icon;
            return (
              <li key={d.id}>
                <NavLink
                  to={d.path}
                  title={`${d.label} (g ${d.shortcut})`}
                  aria-keyshortcuts={`g ${d.shortcut}`}
                  className={({ isActive }) =>
                    cx(
                      "relative flex min-h-9 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium no-underline transition-colors",
                      isActive
                        ? "bg-[var(--ua-selected)] text-[var(--ua-fg)] shadow-[inset_2px_0_0_var(--ua-accent)]"
                        : "text-[var(--ua-muted)] hover:bg-[var(--ua-hover)] hover:text-[var(--ua-fg)]",
                      focusRing,
                    )
                  }
                >
                  <Icon aria-hidden="true" size={18} strokeWidth={1.75} className="shrink-0" />
                  <span className={labelClass}>{d.label}</span>
                  {d.id === "inbox" && (
                    <span
                      className={cx(
                        "inline-flex h-[18px] min-w-5 items-center justify-center rounded-full bg-[var(--ua-decision-surface)] px-1.5 font-mono text-[11px] font-medium text-[var(--ua-decision)]",
                        collapsed ? "absolute right-0 top-0" : "ml-auto max-xl:absolute max-xl:right-0 max-xl:top-0",
                      )}
                    >
                      <span aria-hidden="true">{inbox.text}</span>
                      <span className="sr-only">{inbox.known ? `, ${inbox.count} waiting` : ", waiting count unknown"}</span>
                    </span>
                  )}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="flex-1" />
      <IconButton
        onClick={onToggleCollapsed}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-keyshortcuts="["
        title={collapsed ? "Expand sidebar ([)" : "Collapse sidebar ([)"}
        className="hidden self-start xl:inline-flex"
        icon={collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
      />
    </aside>
  );
}
