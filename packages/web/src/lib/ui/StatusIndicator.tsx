import React from "react";

export type StatusType =
  | "online"
  | "idle"
  | "offline"
  | "open"
  | "doing"
  | "blocked"
  | "done"
  | "running"
  | "waiting"
  | "error"
  | "pending"
  | "approved"
  | "denied"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "neutral"
  | "LIVE"
  | "DORMANT"
  | "MISCONFIGURED";

export type StatusSize = "sm" | "md" | "lg";

export interface StatusIndicatorProps extends React.HTMLAttributes<HTMLSpanElement> {
  status?: StatusType | string | null;
  label?: string;
  pulse?: boolean;
  showGlyph?: boolean;
  size?: StatusSize;
}

interface StatusConfig {
  tokenClass: string;
  glyph: string;
  defaultLabel: string;
}

function getStatusConfig(status?: string | null): StatusConfig {
  const safeStatus = (status ?? "").trim();
  const normalized = safeStatus.toLowerCase();
  const defaultLabel = safeStatus ? safeStatus.toUpperCase() : "UNKNOWN";

  switch (normalized) {
    case "running":
    case "online":
    case "live":
    case "success":
    case "ok":
      return {
        tokenClass: "bg-[var(--ua-ok)]",
        glyph: "●",
        defaultLabel,
      };
    case "done":
    case "completed":
    case "approved":
      return {
        tokenClass: "bg-[var(--ua-ok)]",
        glyph: "✓",
        defaultLabel,
      };
    case "doing":
    case "in_progress":
    case "open":
      return {
        tokenClass: "bg-[var(--ua-live)]",
        glyph: "◐",
        defaultLabel,
      };
    case "error":
    case "blocked":
    case "failed":
    case "denied":
    case "danger":
    case "misconfigured":
      // Failure states read as danger, not as "needs a look" amber.
      return {
        tokenClass: "bg-[var(--ua-danger)]",
        glyph: "▲",
        defaultLabel,
      };
    case "waiting":
      // Waiting on someone is not running work (protocol §3.1).
      return {
        tokenClass: "bg-[var(--ua-warn)]",
        glyph: "◐",
        defaultLabel,
      };
    case "warning":
    case "warn":
    case "pending":
      return {
        tokenClass: "bg-[var(--ua-warn)]",
        glyph: "▲",
        defaultLabel,
      };
    case "idle":
    case "dormant":
    case "offline":
    case "neutral":
    case "muted":
    default:
      return {
        tokenClass: "bg-[var(--ua-muted)]",
        glyph: "○",
        defaultLabel,
      };
  }
}

export const StatusIndicator: React.FC<StatusIndicatorProps> = ({
  status,
  label,
  pulse = false,
  showGlyph = true,
  size = "md",
  className = "",
  ...rest
}) => {
  const config = getStatusConfig(status);
  const displayLabel = label !== undefined ? label : config.defaultLabel;

  const sizeClasses: Record<StatusSize, string> = {
    sm: "h-2.5 w-2.5 text-[7px]",
    md: "h-3 w-3 text-[8px]",
    lg: "h-3.5 w-3.5 text-[9px]",
  };

  const dotClasses = [
    "inline-flex items-center justify-center rounded-full leading-none text-[var(--ua-bg)] font-bold shrink-0",
    sizeClasses[size] ?? sizeClasses.md,
    config.tokenClass,
    pulse && "animate-pulse motion-reduce:animate-none",
  ]
    .filter(Boolean)
    .join(" ");

  const containerClasses = [
    "inline-flex items-center gap-1.5 select-none",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={containerClasses} {...rest}>
      <span aria-hidden="true" className={dotClasses}>
        {showGlyph ? config.glyph : ""}
      </span>
      <span className="text-xs font-semibold uppercase tracking-[0.06em] text-[var(--ua-fg)]">
        {displayLabel}
      </span>
    </span>
  );
};

StatusIndicator.displayName = "StatusIndicator";
