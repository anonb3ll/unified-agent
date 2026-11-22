import type { ReactNode } from "react";
import { Button } from "./Button";
import { cx } from "./cx";
import { Field } from "./Field";
import { Input, Select, type SelectOption } from "./Input";

export type ToolbarProps = {
  /** Names the group for assistive tech, e.g. "Task list controls". */
  label: string;
  children: ReactNode;
  className?: string;
};

/** A row of controls above a list or detail. Wraps on narrow screens. */
export function Toolbar({ label, children, className }: ToolbarProps) {
  return (
    <div role="group" aria-label={label} className={cx("flex flex-wrap items-end gap-2", className)}>
      {children}
    </div>
  );
}

export type FilterDef = {
  id: string;
  label: string;
  /** Current value; "" means no filter. */
  value: string;
  options: readonly SelectOption[];
  /** Text of the "no filter" option. */
  anyLabel?: string;
  onChange: (value: string) => void;
};

export type FilterBarProps = {
  label: string;
  search?: {
    label: string;
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  };
  filters?: readonly FilterDef[];
  /** Clears search and every filter. Shown when anything is set. */
  onClearAll?: () => void;
  /** Extra controls (view switch, "mine"). */
  children?: ReactNode;
  className?: string;
};

/**
 * Search plus filters, with the active filters repeated as removable chips.
 * Controlled: bind values to URL params (`useUrlParams` in the app) so a
 * reload or a shared link restores the view.
 */
export function FilterBar({ label, search, filters = [], onClearAll, children, className }: FilterBarProps) {
  const active = filters.filter((f) => f.value !== "");
  const anything = active.length > 0 || Boolean(search?.value);
  return (
    <div className={cx("flex flex-col gap-2", className)}>
      <Toolbar label={label}>
        {search && (
          <Field label={search.label} labelHidden className="min-w-48 flex-1">
            <Input
              type="search"
              value={search.value}
              placeholder={search.placeholder ?? search.label}
              onChange={(event) => search.onChange(event.target.value)}
            />
          </Field>
        )}
        {filters.map((filter) => (
          <Field key={filter.id} label={filter.label} className="min-w-36">
            <Select
              value={filter.value}
              placeholder={filter.anyLabel ?? "Any"}
              options={filter.options}
              onChange={(event) => filter.onChange(event.target.value)}
            />
          </Field>
        ))}
        {children}
      </Toolbar>
      {anything && (
        <div className="flex flex-wrap items-center gap-2" aria-label="Active filters" role="group">
          {active.map((filter) => {
            const option = filter.options.find((o) => o.value === filter.value);
            return (
              <span
                key={filter.id}
                className="inline-flex items-center gap-1 rounded border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] py-0.5 pl-2 text-xs text-[var(--ua-fg)]"
              >
                {filter.label}: {option?.label ?? filter.value}
                <button
                  type="button"
                  aria-label={`Remove filter ${filter.label}: ${option?.label ?? filter.value}`}
                  onClick={() => filter.onChange("")}
                  className="inline-grid min-h-6 min-w-6 cursor-pointer place-items-center rounded border-0 bg-transparent text-[var(--ua-fg)] hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] max-md:min-h-11 max-md:min-w-11"
                >
                  <span aria-hidden="true">×</span>
                </button>
              </span>
            );
          })}
          {onClearAll && (
            <Button size="sm" variant="ghost" onClick={onClearAll}>
              Clear all
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
