import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { buttonClassName, type ButtonSize, type ButtonVariant } from "./Button";
import { cx } from "./cx";
import { UiLink } from "./link";

export type MenuAction = {
  kind?: "item";
  id: string;
  label: string;
  icon?: ReactNode;
  /** Runs the item. Ignored when `href` is set. */
  onSelect?: () => void;
  /** Makes the item a link (`<a role="menuitem">`). */
  href?: string;
  reloadDocument?: boolean;
  disabled?: boolean;
  /** Set for one-of-many items (theme): the item renders as `menuitemradio`. */
  checked?: boolean;
  /** Keyboard shortcut hint, shown on the right. */
  shortcut?: string;
};

export type MenuEntry =
  | MenuAction
  | { kind: "separator"; id: string }
  | { kind: "label"; id: string; label: string };

export type MenuProps = {
  /** Visible trigger content. */
  label: ReactNode;
  /** Accessible name when the trigger has no text (icon only) or needs more context. */
  "aria-label"?: string;
  items: readonly MenuEntry[];
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Which edge of the trigger the menu lines up with. */
  align?: "start" | "end";
  /** Open above the trigger (bottom bars). */
  placement?: "bottom" | "top";
  triggerClassName?: string;
  /** Extra attributes for the trigger, such as `aria-keyshortcuts`. */
  triggerProps?: Record<string, string | undefined>;
  className?: string;
};

function isAction(entry: MenuEntry): entry is MenuAction {
  return entry.kind === undefined || entry.kind === "item";
}

/**
 * Button plus menu (WAI-ARIA menu button). Enter, Space or Down opens on the
 * first item, Up on the last; arrows, Home and End move; typing a letter jumps
 * to the next item starting with it; Escape closes and returns focus to the
 * button; Tab closes and moves on.
 */
export function Menu({
  label,
  items,
  variant = "secondary",
  size = "md",
  align = "start",
  placement = "bottom",
  triggerClassName,
  triggerProps,
  className,
  ...aria
}: MenuProps) {
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLElement | null>>([]);
  const typeahead = useRef({ text: "", at: 0 });
  const baseId = useId();
  const menuId = `${baseId}-menu`;
  const triggerId = `${baseId}-trigger`;

  const actions = items.filter(isAction);
  const enabled = actions.map((a, i) => (a.disabled ? -1 : i)).filter((i) => i >= 0);

  useEffect(() => {
    if (open) itemRefs.current[focusIndex]?.focus();
  }, [open, focusIndex]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const openAt = (which: "first" | "last") => {
    if (enabled.length === 0) return;
    setFocusIndex(which === "first" ? enabled[0] : enabled[enabled.length - 1]);
    setOpen(true);
  };

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  };

  const move = (delta: 1 | -1) => {
    const at = enabled.indexOf(focusIndex);
    const next = enabled[(at + delta + enabled.length) % enabled.length];
    setFocusIndex(next);
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openAt("first");
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openAt("last");
    }
  };

  const onMenuKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        move(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        return;
      case "Home":
        event.preventDefault();
        setFocusIndex(enabled[0]);
        return;
      case "End":
        event.preventDefault();
        setFocusIndex(enabled[enabled.length - 1]);
        return;
      case "Escape":
        event.preventDefault();
        event.stopPropagation();
        close(true);
        return;
      case "Tab":
        close(false);
        return;
    }
    if (event.key.length === 1 && /\S/.test(event.key) && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const now = Date.now();
      const buffer = now - typeahead.current.at < 600 ? typeahead.current.text + event.key : event.key;
      typeahead.current = { text: buffer.toLowerCase(), at: now };
      const at = enabled.indexOf(focusIndex);
      const order = [...enabled.slice(at + 1), ...enabled.slice(0, at + 1)];
      const prefix = typeahead.current.text;
      const hit =
        order.find((i) => actions[i].label.toLowerCase().startsWith(prefix)) ??
        order.find((i) => actions[i].label.toLowerCase().startsWith(prefix[0]));
      if (hit !== undefined) {
        event.preventDefault();
        setFocusIndex(hit);
      }
    }
  };

  const select = (action: MenuAction) => {
    if (action.disabled) return;
    close(!action.href);
    action.onSelect?.();
  };

  let actionIndex = -1;
  const itemClass = (disabled?: boolean) =>
    cx(
      "flex w-full cursor-pointer items-center gap-2 rounded border-0 bg-transparent px-2.5 py-1.5 text-left text-sm text-[var(--ua-fg)] no-underline",
      "hover:bg-[var(--ua-hover)] focus:bg-[var(--ua-selected)] focus:outline-none focus-visible:outline-2 focus-visible:outline-[var(--ua-accent)] max-md:min-h-11",
      disabled && "cursor-not-allowed opacity-50",
    );

  return (
    <div ref={rootRef} className={cx("relative inline-block", className)}>
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={aria["aria-label"]}
        onClick={() => (open ? close(false) : openAt("first"))}
        onKeyDown={onTriggerKeyDown}
        className={buttonClassName(variant, size, triggerClassName)}
        {...triggerProps}
      >
        {label}
      </button>
      {open && (
        <ul
          id={menuId}
          role="menu"
          aria-labelledby={triggerId}
          onKeyDown={onMenuKeyDown}
          className={cx(
            "absolute z-40 m-0 min-w-52 list-none rounded-md border border-[var(--ua-border-strong)] bg-[var(--ua-raised)] p-1 shadow-xl",
            align === "end" ? "right-0" : "left-0",
            placement === "top" ? "bottom-full mb-1" : "top-full mt-1",
          )}
        >
          {items.map((entry) => {
            if (entry.kind === "separator") {
              return <li key={entry.id} role="separator" className="my-1 h-px bg-[var(--ua-border)]" />;
            }
            if (entry.kind === "label") {
              return (
                <li key={entry.id} role="presentation" className="px-2.5 pb-1 pt-2 text-xs font-medium text-[var(--ua-muted)]">
                  {entry.label}
                </li>
              );
            }
            actionIndex += 1;
            const index = actionIndex;
            const role = entry.checked === undefined ? "menuitem" : "menuitemradio";
            const content = (
              <>
                <span aria-hidden="true" className="inline-flex w-4 shrink-0 justify-center text-[var(--ua-accent)]">
                  {entry.checked ? "✓" : entry.icon}
                </span>
                <span className="flex-1">{entry.label}</span>
                {entry.shortcut && (
                  <kbd aria-hidden="true" className="font-mono text-[11px] text-[var(--ua-muted)]">
                    {entry.shortcut}
                  </kbd>
                )}
              </>
            );
            const common = {
              role,
              tabIndex: -1,
              "aria-checked": entry.checked,
              "aria-disabled": entry.disabled || undefined,
              "aria-keyshortcuts": entry.shortcut,
              className: itemClass(entry.disabled),
            };
            return (
              <li key={entry.id} role="none">
                {entry.href && !entry.disabled ? (
                  <UiLink
                    {...common}
                    ref={(el) => {
                      itemRefs.current[index] = el;
                    }}
                    href={entry.href}
                    reloadDocument={entry.reloadDocument}
                    onClick={() => select(entry)}
                  >
                    {content}
                  </UiLink>
                ) : (
                  <button
                    {...common}
                    ref={(el) => {
                      itemRefs.current[index] = el;
                    }}
                    type="button"
                    onClick={() => select(entry)}
                  >
                    {content}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
