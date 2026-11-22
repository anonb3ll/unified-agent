import { useId, type ReactNode } from "react";
import { Button } from "./Button";
import { cx } from "./cx";

type HeadingLevel = 2 | 3 | 4;

function Heading({ level, children, id }: { level: HeadingLevel; children: ReactNode; id?: string }) {
  const Tag = `h${level}` as const;
  return (
    <Tag id={id} className="m-0 text-base font-semibold text-[var(--ua-fg)]">
      {children}
    </Tag>
  );
}

export type EmptyStateProps = {
  /** What this region is, when it has nothing: "No tasks match these filters". */
  title: ReactNode;
  /** Why it is empty and what the person can do about it. */
  description?: ReactNode;
  /** One primary action (a `Button` or `LinkButton`). */
  action?: ReactNode;
  icon?: ReactNode;
  headingLevel?: HeadingLevel;
  className?: string;
};

/** A complete read that returned nothing. Never used while loading or after an error. */
export function EmptyState({ title, description, action, icon, headingLevel = 2, className }: EmptyStateProps) {
  return (
    <div
      className={cx(
        "flex flex-col items-center gap-2 rounded-lg border border-dashed border-[var(--ua-border-strong)] px-6 py-10 text-center",
        className,
      )}
    >
      {icon && (
        <span aria-hidden="true" className="text-[var(--ua-muted)]">
          {icon}
        </span>
      )}
      <Heading level={headingLevel}>{title}</Heading>
      {description && <p className="m-0 max-w-[60ch] text-sm text-[var(--ua-muted)]">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export type ErrorStateProps = {
  title?: ReactNode;
  /** What happened and what to do next, in plain words. */
  message: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  /** The raw transport message; shown only when the person opens it. */
  detail?: string | null;
  headingLevel?: HeadingLevel;
  className?: string;
};

/** A failed read or write. The raw error goes in the collapsed detail, never the headline. */
export function ErrorState({
  title = "This could not be loaded",
  message,
  onRetry,
  retryLabel = "Retry",
  detail,
  headingLevel = 2,
  className,
}: ErrorStateProps) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      className={cx(
        "flex flex-col gap-2 rounded-lg border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] px-4 py-3",
        className,
      )}
    >
      <Heading level={headingLevel} id={headingId}>
        <span aria-hidden="true" className="mr-1.5 text-[var(--ua-danger)]">
          ▲
        </span>
        {title}
      </Heading>
      <p className="m-0 text-sm text-[var(--ua-fg)]">{message}</p>
      <div className="flex flex-wrap items-center gap-3">
        {onRetry && (
          <Button size="sm" variant="secondary" onClick={onRetry}>
            {retryLabel}
          </Button>
        )}
        {detail && (
          <details className="text-xs text-[var(--ua-fg)]">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
          </details>
        )}
      </div>
    </section>
  );
}

export type SkeletonProps = {
  /** Announced to assistive tech, e.g. "Loading tasks". */
  label?: string;
  lines?: number;
  /** `block` draws one tall placeholder (a card or chart). */
  variant?: "lines" | "block";
  className?: string;
};

/** Loading placeholder. It never shows a number, so a pending read cannot look like zero. */
export function Skeleton({ label = "Loading", lines = 3, variant = "lines", className }: SkeletonProps) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={cx("flex flex-col gap-2", className)}>
      <span className="sr-only">{label}…</span>
      {variant === "block" ? (
        <span aria-hidden="true" className="block h-24 animate-pulse rounded-md bg-[var(--ua-hover)]" />
      ) : (
        Array.from({ length: lines }, (_, i) => (
          <span
            key={i}
            aria-hidden="true"
            className="block h-3.5 animate-pulse rounded bg-[var(--ua-hover)]"
            style={{ width: `${[92, 76, 64, 84][i % 4]}%` }}
          />
        ))
      )}
    </div>
  );
}
