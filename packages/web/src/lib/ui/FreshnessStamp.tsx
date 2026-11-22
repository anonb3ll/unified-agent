import { useEffect, useState } from "react";
import { formatAge, type TruthState } from "./truthState";

export interface FreshnessStampProps {
  state: TruthState;
  updatedAt: number | null;
  /** Injected clock for tests; defaults to a live clock. */
  now?: number;
  className?: string;
}

/**
 * How current a region is (design protocol §6.2): "updated 4s ago",
 * "stale · 26h", "partial". The exact time is in the title.
 */
export function FreshnessStamp({ state, updatedAt, now, className = "" }: FreshnessStampProps) {
  const [clock, setClock] = useState(Date.now);
  useEffect(() => {
    if (now !== undefined || updatedAt === null) return;
    const timer = setInterval(() => setClock(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [now, updatedAt]);
  const at = now ?? clock;
  const age = updatedAt !== null ? formatAge(at - updatedAt) : null;
  let text: string;
  let tone = "text-[var(--ua-faint)]";
  switch (state) {
    case "loading":
      text = "loading…";
      break;
    case "ready":
    case "empty":
      text = age ? `updated ${age} ago` : "updated";
      break;
    case "stale":
      text = age ? `stale · ${age}` : "stale";
      tone = "text-[var(--ua-warn)]";
      break;
    case "partial":
      text = "partial";
      tone = "text-[var(--ua-warn)]";
      break;
    case "denied":
      text = "no access";
      break;
    case "unavailable":
      text = "unavailable";
      tone = "text-[var(--ua-danger)]";
      break;
  }
  const title = updatedAt !== null ? `Last updated ${new Date(updatedAt).toISOString()}` : undefined;
  return (
    <span className={`inline-flex items-center text-xs ${className}`} title={title}>
      <span className={tone}>{text}</span>
    </span>
  );
}
