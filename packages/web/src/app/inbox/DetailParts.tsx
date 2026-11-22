import type { ReactNode } from "react";
import { Badge, type BadgeVariant } from "../../lib/ui";
import { describeReadError } from "../lib/queries";
import { ErrorState, Skeleton } from "../../lib/ui";

/** Top of the detail pane: what the item is, as the pane's heading (focused on open). */
export function DetailHeader({
  kind,
  title,
  status,
  statusVariant = "neutral",
  meta,
}: {
  kind: string;
  title: ReactNode;
  status?: string;
  statusVariant?: BadgeVariant;
  meta?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-1 border-b border-[var(--ua-border)] pb-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ua-muted)]">
        <span>{kind}</span>
        {status && <Badge variant={statusVariant}>{status}</Badge>}
        {meta}
      </div>
      <h2 tabIndex={-1} data-detail-heading="" className="m-0 break-words text-lg font-semibold text-[var(--ua-fg)] focus:outline-none">
        {title}
      </h2>
    </header>
  );
}

export function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="m-0 text-sm font-semibold text-[var(--ua-fg)]">{title}</h3>
      {children}
    </section>
  );
}

/** A read inside the detail pane in exactly one of loading / error with Retry / ready. */
export function DetailRead<T>({
  query,
  label,
  children,
  notFound,
}: {
  query: { status: "pending" | "error" | "success"; data?: T; error: unknown; refetch: () => unknown };
  label: string;
  children: (data: T) => ReactNode;
  /** Shown instead of an error when the hub answers 404. */
  notFound?: ReactNode;
}) {
  if (query.status === "pending") return <Skeleton label={`Loading ${label}`} />;
  if (query.status === "error" || query.data === undefined) {
    const { message, detail } = describeReadError(query.error);
    if (notFound && /HTTP 404/.test(String(detail))) return <>{notFound}</>;
    return <ErrorState headingLevel={3} title={`The ${label} could not be loaded`} message={message} detail={detail} onRetry={() => void query.refetch()} />;
  }
  return <>{children(query.data)}</>;
}

/** Plain-text rendering of agent-written text: never HTML or Markdown. */
export function PlainText({ text, max = 8000 }: { text: string; max?: number }) {
  const shown = text.length > max ? `${text.slice(0, max)}…` : text;
  return <p className="m-0 whitespace-pre-wrap break-words text-sm text-[var(--ua-fg)]">{shown}</p>;
}

/** One-line rendering of an argument value for the summary. */
export function summarizeValue(value: unknown, max = 140): string {
  let text: string;
  if (value === null || value === undefined) text = "none";
  else if (typeof value === "string") text = value;
  else if (typeof value === "number" || typeof value === "boolean") text = String(value);
  else if (Array.isArray(value)) text = `${value.length} item${value.length === 1 ? "" : "s"}: ${JSON.stringify(value)}`;
  else text = JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
