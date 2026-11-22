import { useCallback, useId, useRef, type KeyboardEvent } from "react";
import { useTheme, type ThemeMode } from "../theme/ThemeProvider";

const OPTIONS: { id: ThemeMode; label: string; hint: string }[] = [
  { id: "light", label: "Light", hint: "Light surface theme" },
  { id: "dark", label: "Dark", hint: "Dark surface theme" },
  { id: "high-contrast", label: "High contrast", hint: "High contrast maximum contrast theme" },
];

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const labelId = useId();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const focusIndex = useCallback((index: number) => {
    refs.current[index]?.focus();
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const current = OPTIONS.findIndex((item) => item.id === theme);
      if (current < 0) return;

      let next = current;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        next = (current + 1) % OPTIONS.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        next = (current - 1 + OPTIONS.length) % OPTIONS.length;
      } else if (event.key === "Home") {
        next = 0;
      } else if (event.key === "End") {
        next = OPTIONS.length - 1;
      } else {
        return;
      }

      event.preventDefault();
      setTheme(OPTIONS[next].id);
      focusIndex(next);
    },
    [focusIndex, setTheme, theme],
  );

  return (
    <div>
      <p
        id={labelId}
        className="m-0 mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-[var(--ua-muted)]"
      >
        Theme
      </p>
      <div
        role="radiogroup"
        aria-labelledby={labelId}
        className="flex flex-wrap gap-1"
        onKeyDown={onKeyDown}
      >
        {OPTIONS.map((opt, index) => {
          const selected = theme === opt.id;
          return (
            <button
              key={opt.id}
              ref={(el) => {
                refs.current[index] = el;
              }}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={`${opt.label}: ${opt.hint}`}
              tabIndex={selected ? 0 : -1}
              className={
                selected
                  ? "rounded border border-[var(--ua-accent)] bg-[var(--ua-accent)] px-2.5 py-1.5 text-xs font-medium text-[var(--ua-accent-fg)]"
                  : "rounded border border-[var(--ua-border)] bg-[var(--ua-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]"
              }
              onClick={() => setTheme(opt.id)}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
