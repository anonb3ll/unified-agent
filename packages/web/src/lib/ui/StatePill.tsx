import React from "react";

export type StateKind = "decision" | "danger" | "live" | "warn" | "ok" | "unknown";

const KINDS: Record<StateKind, { glyph: string; word: string; className: string }> = {
  decision: {
    glyph: "◆",
    word: "Decision",
    className: "bg-[var(--ua-decision-surface)] text-[var(--ua-decision)]",
  },
  danger: {
    glyph: "▲",
    word: "Blocked",
    className: "bg-[var(--ua-danger-surface)] text-[var(--ua-danger)]",
  },
  live: { glyph: "◐", word: "Running", className: "bg-[var(--ua-live-surface)] text-[var(--ua-live)]" },
  warn: { glyph: "◷", word: "Waiting", className: "bg-[var(--ua-warn-surface)] text-[var(--ua-warn)]" },
  ok: { glyph: "✓", word: "Verified", className: "bg-[var(--ua-ok-surface)] text-[var(--ua-ok)]" },
  unknown: {
    glyph: "?",
    word: "Unknown",
    className: "text-[var(--ua-muted)] ring-1 ring-inset ring-[var(--ua-unknown)]",
  },
};

/** Unknown is hatched so it never reads as idle or as zero (design protocol §6.1). */
const HATCH: React.CSSProperties = {
  backgroundImage:
    "repeating-linear-gradient(135deg, transparent 0 4px, color-mix(in oklab, var(--ua-unknown) 22%, transparent) 4px 5px)",
};

export interface StatePillProps extends React.HTMLAttributes<HTMLSpanElement> {
  kind: StateKind;
  /** Overrides the default word; keep it short and plain. */
  label?: string;
}

/**
 * One of the six closed states: a colour, a glyph and an uppercase word, so
 * meaning survives high contrast (where most states are yellow) and colour
 * blindness. A new status maps onto one of these.
 */
export const StatePill: React.FC<StatePillProps> = ({ kind, label, className = "", style, ...rest }) => {
  const k = KINDS[kind];
  return (
    <span
      data-state={kind}
      className={[
        "inline-flex h-[22px] w-max items-center gap-1.5 whitespace-nowrap rounded px-2 text-[11px] font-medium uppercase leading-none tracking-[0.04em]",
        k.className,
        className,
      ].join(" ")}
      style={kind === "unknown" ? { ...HATCH, ...style } : style}
      {...rest}
    >
      <span aria-hidden="true">{k.glyph}</span>
      {label ?? k.word}
    </span>
  );
};
