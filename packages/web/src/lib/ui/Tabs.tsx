import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { cx, focusRing } from "./cx";
import { UiLink } from "./link";

export type LinkTab = {
  href: string;
  label: string;
  /** Marks the tab for the current page (`aria-current="page"`). */
  current?: boolean;
  /** Count from a complete read; pass "–" when unknown. */
  count?: number | string;
};

export type LinkTabsProps = {
  /** Names the navigation landmark, e.g. "Work sections". */
  label: string;
  items: readonly LinkTab[];
  className?: string;
};

const tabClass = (selected: boolean) =>
  cx(
    "inline-flex min-h-9 items-center gap-1.5 whitespace-nowrap border-0 border-b-2 bg-transparent px-3 text-sm font-medium no-underline max-md:min-h-11",
    selected
      ? "border-[var(--ua-accent)] text-[var(--ua-fg)]"
      : "border-transparent text-[var(--ua-muted)] hover:text-[var(--ua-fg)]",
    focusRing,
  );

/**
 * Section tabs where each tab is its own URL. A `<nav>` of links, not a
 * tablist, so back, forward and "open in new tab" behave like links.
 */
export function LinkTabs({ label, items, className }: LinkTabsProps) {
  return (
    <nav aria-label={label} className={cx("border-b border-[var(--ua-border)]", className)}>
      <ul className="m-0 flex list-none gap-1 overflow-x-auto p-0">
        {items.map((item) => (
          <li key={item.href}>
            <UiLink href={item.href} aria-current={item.current ? "page" : undefined} className={tabClass(Boolean(item.current))}>
              {item.label}
              {item.count !== undefined && (
                <span className="rounded-full bg-[var(--ua-hover)] px-1.5 font-mono text-[11px] text-[var(--ua-fg)]">
                  {item.count}
                </span>
              )}
            </UiLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export type TabItem = {
  id: string;
  label: string;
  panel: ReactNode;
  disabled?: boolean;
};

export type TabsProps = {
  /** Names the tablist. */
  label: string;
  items: readonly TabItem[];
  /** Selected tab id; omit for uncontrolled. Bind to a URL param for shareable state. */
  value?: string;
  defaultValue?: string;
  onChange?: (id: string) => void;
  className?: string;
};

/**
 * In-page tabs (WAI-ARIA tabs, automatic activation): Left/Right move and
 * select, Home/End jump, only the selected tab is in the tab order.
 */
export function Tabs({ label, items, value, defaultValue, onChange, className }: TabsProps) {
  const [inner, setInner] = useState(defaultValue ?? items[0]?.id);
  const selected = value ?? inner;
  const baseId = useId();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const enabled = items.map((t, i) => (t.disabled ? -1 : i)).filter((i) => i >= 0);

  const choose = (index: number) => {
    const item = items[index];
    if (!item || item.disabled) return;
    if (value === undefined) setInner(item.id);
    onChange?.(item.id);
    refs.current[index]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const current = items.findIndex((t) => t.id === selected);
    const at = enabled.indexOf(current);
    let next: number | undefined;
    if (event.key === "ArrowRight") next = enabled[(at + 1) % enabled.length];
    else if (event.key === "ArrowLeft") next = enabled[(at - 1 + enabled.length) % enabled.length];
    else if (event.key === "Home") next = enabled[0];
    else if (event.key === "End") next = enabled[enabled.length - 1];
    if (next === undefined) return;
    event.preventDefault();
    choose(next);
  };

  return (
    <div className={className}>
      <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className="flex gap-1 overflow-x-auto border-b border-[var(--ua-border)]">
        {items.map((item, index) => {
          const isSelected = item.id === selected;
          return (
            <button
              key={item.id}
              ref={(el) => {
                refs.current[index] = el;
              }}
              id={`${baseId}-tab-${item.id}`}
              type="button"
              role="tab"
              aria-selected={isSelected}
              aria-controls={`${baseId}-panel-${item.id}`}
              tabIndex={isSelected ? 0 : -1}
              disabled={item.disabled}
              onClick={() => choose(index)}
              className={cx(tabClass(isSelected), "cursor-pointer disabled:cursor-not-allowed disabled:opacity-50")}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      {items.map((item) => (
        <div
          key={item.id}
          id={`${baseId}-panel-${item.id}`}
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-${item.id}`}
          hidden={item.id !== selected}
          tabIndex={0}
          className="pt-4 focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
        >
          {item.id === selected ? item.panel : null}
        </div>
      ))}
    </div>
  );
}
