import { NavLink } from "react-router";
import { MoreHorizontal } from "lucide-react";
import { cx, focusRing, Menu } from "../../lib/ui";
import { DESTINATIONS, PHONE_TABS } from "../routes";
import { useInboxCount } from "./InboxCount";

/** Phone navigation (spec §4): Home, Inbox, Work, Sessions and "More". */
export function BottomTabBar() {
  const inbox = useInboxCount();
  const tabs = DESTINATIONS.filter((d) => PHONE_TABS.includes(d.id));
  const more = DESTINATIONS.filter((d) => !PHONE_TABS.includes(d.id));
  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-20 border-t border-[var(--ua-border)] bg-[var(--ua-surface)] pb-[env(safe-area-inset-bottom)] md:hidden"
    >
      <ul className="m-0 grid list-none grid-cols-5 p-0">
        {tabs.map((d) => {
          const Icon = d.icon;
          return (
            <li key={d.id}>
              <NavLink
                to={d.path}
                className={({ isActive }) =>
                  cx(
                    "relative flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px] font-medium no-underline",
                    isActive ? "text-[var(--ua-fg)]" : "text-[var(--ua-muted)]",
                    focusRing,
                  )
                }
              >
                <Icon aria-hidden="true" size={20} strokeWidth={1.75} />
                {d.label}
                {d.id === "inbox" && (
                  <span className="absolute right-2 top-1 rounded-full bg-[var(--ua-decision-surface)] px-1 font-mono text-[10px] text-[var(--ua-decision)]">
                    <span aria-hidden="true">{inbox.text}</span>
                    <span className="sr-only">{inbox.known ? `, ${inbox.count} waiting` : ", waiting count unknown"}</span>
                  </span>
                )}
              </NavLink>
            </li>
          );
        })}
        <li className="flex items-stretch justify-center">
          <Menu
            label={
              <span className="flex flex-col items-center gap-0.5 text-[11px]">
                <MoreHorizontal aria-hidden="true" size={20} strokeWidth={1.75} />
                More
              </span>
            }
            aria-label="More destinations"
            variant="ghost"
            placement="top"
            align="end"
            className="flex w-full"
            triggerClassName="!min-h-14 w-full !text-[var(--ua-muted)] !border-0 !rounded-none"
            items={more.map((d) => ({ id: d.id, label: d.label, href: d.path }))}
          />
        </li>
      </ul>
    </nav>
  );
}
