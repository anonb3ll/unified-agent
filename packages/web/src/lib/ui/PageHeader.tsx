import type { ReactNode } from "react";
import { cx } from "./cx";

export type PageHeaderProps = {
  title: ReactNode;
  description?: ReactNode;
  /** Buttons for the page's main actions, right aligned (stacked on phones). */
  actions?: ReactNode;
  /** Section tabs (`LinkTabs`) rendered under the header. */
  tabs?: ReactNode;
  /** Small line above the title (status, id). */
  meta?: ReactNode;
  className?: string;
};

/**
 * The top of every page: its one `h1`, a sentence on what the page is for,
 * and the actions. The heading takes programmatic focus after navigation
 * (`tabIndex={-1}`), so screen readers announce the new page.
 */
export function PageHeader({ title, description, actions, tabs, meta, className }: PageHeaderProps) {
  return (
    <header className={cx("mb-5 flex flex-col gap-3", className)}>
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          {meta && <div className="mb-1 text-xs text-[var(--ua-muted)]">{meta}</div>}
          <h1
            tabIndex={-1}
            data-page-heading=""
            className="m-0 text-xl font-semibold tracking-[-0.015em] text-[var(--ua-fg)] focus:outline-none"
          >
            {title}
          </h1>
          {description && <p className="m-0 mt-1 max-w-[70ch] text-sm text-[var(--ua-muted)]">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2 max-sm:w-full">{actions}</div>}
      </div>
      {tabs}
    </header>
  );
}
