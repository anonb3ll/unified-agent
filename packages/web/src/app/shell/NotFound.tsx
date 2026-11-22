import { useLocation } from "react-router";
import { PageHeader, UiLink } from "../../lib/ui";
import { DESTINATIONS } from "../routes";

/** `*`: a path no area claims. Lists the eight destinations (spec §3). */
export function NotFound() {
  const { pathname } = useLocation();
  return (
    <>
      <PageHeader
        title="Page not found"
        description={`Nothing lives at ${pathname}. The link may be old, or the page may have moved. Pick where to go:`}
      />
      <ul className="m-0 grid list-none grid-cols-1 gap-2 p-0 sm:grid-cols-2">
        {DESTINATIONS.map((d) => {
          const Icon = d.icon;
          return (
            <li key={d.id}>
              <UiLink
                href={d.path}
                className="flex items-start gap-3 rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] p-3 text-[var(--ua-fg)] no-underline hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)]"
              >
                <Icon aria-hidden="true" size={18} strokeWidth={1.75} className="mt-0.5 shrink-0" />
                <span>
                  <span className="block font-medium">{d.label}</span>
                  <span className="block text-sm text-[var(--ua-muted)]">{d.description}</span>
                </span>
              </UiLink>
            </li>
          );
        })}
      </ul>
    </>
  );
}
