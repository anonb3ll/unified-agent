import type { ReactNode } from "react";
import { cx } from "./cx";

export type DescriptionItem = { term: ReactNode; value: ReactNode; id?: string };

export type DescriptionListProps = {
  items: readonly DescriptionItem[];
  /** Two columns of pairs on wide screens. */
  columns?: 1 | 2;
  className?: string;
};

/** Label and value pairs on a detail page. A missing value reads "Not set", never blank. */
export function DescriptionList({ items, columns = 1, className }: DescriptionListProps) {
  return (
    <dl className={cx("m-0 grid gap-x-6 gap-y-3", columns === 2 && "md:grid-cols-2", className)}>
      {items.map((item, index) => (
        <div key={item.id ?? index} className="grid grid-cols-[minmax(7rem,35%)_1fr] gap-3 max-sm:grid-cols-1 max-sm:gap-0.5">
          <dt className="text-sm text-[var(--ua-muted)]">{item.term}</dt>
          <dd className="m-0 min-w-0 break-words text-sm text-[var(--ua-fg)]">
            {item.value === null || item.value === undefined || item.value === "" ? (
              <span className="text-[var(--ua-muted)]">Not set</span>
            ) : (
              item.value
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
