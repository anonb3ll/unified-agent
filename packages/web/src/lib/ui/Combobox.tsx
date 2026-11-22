import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { controlClass, cx } from "./cx";
import { useFieldControl } from "./Field";

export type ComboboxOption = {
  value: string;
  label: string;
  /** Second line in the list, also searched. */
  description?: string;
  disabled?: boolean;
};

export type ComboboxProps = {
  options: readonly ComboboxOption[];
  /** Selected value; `null` for none. Omit for an uncontrolled combobox. */
  value?: string | null;
  defaultValue?: string | null;
  onChange?: (value: string | null) => void;
  placeholder?: string;
  /** Shown in the list when the filter matches nothing. */
  emptyText?: ReactNode;
  /** Submitted with a form under this name. */
  name?: string;
  id?: string;
  disabled?: boolean;
  required?: boolean;
  "aria-label"?: string;
  "aria-describedby"?: string;
  className?: string;
};

function matches(option: ComboboxOption, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    option.label.toLowerCase().includes(q) ||
    option.value.toLowerCase().includes(q) ||
    Boolean(option.description?.toLowerCase().includes(q))
  );
}

/**
 * Filterable single select (agent, project, parent task), following the
 * ARIA 1.2 combobox pattern: the input owns focus, the list is referenced by
 * `aria-controls`, and the active option by `aria-activedescendant`.
 * Down/Up move, Enter picks, Escape closes (and clears when already closed).
 */
export function Combobox({
  options,
  value,
  defaultValue = null,
  onChange,
  placeholder,
  emptyText = "No matches",
  name,
  id,
  disabled,
  required,
  className,
  ...aria
}: ComboboxProps) {
  const controlled = value !== undefined;
  const [inner, setInner] = useState<string | null>(defaultValue);
  const selected = controlled ? value : inner;
  const selectedOption = options.find((o) => o.value === selected) ?? null;

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = `${useId()}-list`;
  const fieldProps = useFieldControl({ id, required, "aria-describedby": aria["aria-describedby"] });

  const text = query ?? selectedOption?.label ?? "";
  const visible = useMemo(() => options.filter((o) => matches(o, query ?? "")), [options, query]);
  const activeIndex = Math.min(active, Math.max(visible.length - 1, 0));
  const optionId = (index: number) => `${listId}-${index}`;

  const commit = (option: ComboboxOption | null) => {
    if (option?.disabled) return;
    const next = option?.value ?? null;
    if (!controlled) setInner(next);
    onChange?.(next);
    setQuery(null);
    setOpen(false);
  };

  const openAt = (index: number) => {
    setOpen(true);
    setActive(index);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) openAt(Math.max(visible.findIndex((o) => o.value === selected), 0));
        else setActive((activeIndex + 1) % Math.max(visible.length, 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) openAt(Math.max(visible.length - 1, 0));
        else setActive((activeIndex - 1 + visible.length) % Math.max(visible.length, 1));
        break;
      case "Home":
        if (open) {
          event.preventDefault();
          setActive(0);
        }
        break;
      case "End":
        if (open) {
          event.preventDefault();
          setActive(Math.max(visible.length - 1, 0));
        }
        break;
      case "Enter":
        if (open) {
          event.preventDefault();
          commit(visible[activeIndex] ?? null);
        }
        break;
      case "Escape":
        if (open) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          setQuery(null);
        } else if (selected !== null) {
          event.preventDefault();
          commit(null);
        }
        break;
      case "Tab":
        setOpen(false);
        setQuery(null);
        break;
    }
  };

  const expanded = open && !disabled;
  const activeId = expanded && visible.length > 0 ? optionId(activeIndex) : null;

  // Keep the active option visible as arrows and filtering move it.
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView?.({ block: "nearest" });
  }, [activeId, query]);

  return (
    <div className={cx("relative", className)}>
      <input
        ref={inputRef}
        {...fieldProps}
        type="text"
        role="combobox"
        aria-label={aria["aria-label"]}
        aria-expanded={expanded}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId ?? undefined}
        autoComplete="off"
        disabled={disabled}
        placeholder={placeholder}
        value={text}
        onChange={(event) => {
          setQuery(event.target.value);
          openAt(0);
        }}
        onKeyDown={onKeyDown}
        onClick={() => setOpen(true)}
        onBlur={() => {
          setOpen(false);
          setQuery(null);
        }}
        className={cx(controlClass, "pr-8")}
      />
      <span aria-hidden="true" className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-xs text-[var(--ua-muted)]">
        ▾
      </span>
      {name && <input type="hidden" name={name} value={selected ?? ""} />}
      <ul
        id={listId}
        role="listbox"
        aria-label={aria["aria-label"] ?? "Options"}
        hidden={!expanded}
        className="absolute left-0 right-0 z-30 m-0 mt-1 max-h-64 list-none overflow-auto rounded-md border border-[var(--ua-border-strong)] bg-[var(--ua-raised)] p-1 shadow-lg"
      >
        {visible.length === 0 ? (
          <li role="presentation" className="px-2.5 py-2 text-sm text-[var(--ua-muted)]">
            {emptyText}
          </li>
        ) : (
          visible.map((option, index) => (
            <li
              key={option.value}
              id={optionId(index)}
              role="option"
              aria-selected={option.value === selected}
              aria-disabled={option.disabled || undefined}
              // Keep focus in the input: pick on mousedown, before blur closes the list.
              onMouseDown={(event) => {
                event.preventDefault();
                commit(option);
              }}
              onMouseEnter={() => setActive(index)}
              className={cx(
                "flex cursor-pointer flex-col rounded px-2.5 py-1.5 text-sm text-[var(--ua-fg)] max-md:min-h-11 max-md:justify-center",
                index === activeIndex && "bg-[var(--ua-selected)]",
                option.disabled && "cursor-not-allowed opacity-60",
              )}
            >
              <span className="flex items-center gap-2">
                <span aria-hidden="true" className="w-3 text-[var(--ua-accent)]">
                  {option.value === selected ? "✓" : ""}
                </span>
                {option.label}
              </span>
              {option.description && <span className="pl-5 text-xs text-[var(--ua-muted)]">{option.description}</span>}
            </li>
          ))
        )}
      </ul>
    </div>
  );
}
