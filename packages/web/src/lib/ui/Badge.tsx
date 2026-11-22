import React from "react";

export type BadgeVariant = "neutral" | "info" | "success" | "warning" | "danger";
export type BadgeSize = "sm" | "md";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
  size?: BadgeSize;
  dot?: boolean;
}

export const Badge: React.FC<BadgeProps> = ({
  variant = "neutral",
  size = "sm",
  dot = false,
  className = "",
  children,
  ...rest
}) => {
  const baseClasses =
    "inline-flex items-center font-medium rounded border uppercase tracking-wider select-none";

  const variantClasses: Record<BadgeVariant, string> = {
    neutral: "border-[var(--ua-border)] bg-[var(--ua-surface)] text-[var(--ua-muted)]",
    info: "border-[var(--ua-info)] bg-[var(--ua-surface)] text-[var(--ua-fg)]",
    success: "border-[var(--ua-ok)] bg-[var(--ua-surface)] text-[var(--ua-ok)]",
    warning: "border-[var(--ua-warn)] bg-[var(--ua-surface)] text-[var(--ua-warn)]",
    danger: "border-[var(--ua-danger)] bg-[var(--ua-surface)] text-[var(--ua-danger)]",
  };

  const sizeClasses: Record<BadgeSize, string> = {
    sm: "text-xs px-1.5 py-0.5 gap-1",
    md: "text-sm px-2.5 py-1 gap-1.5",
  };

  const combinedClassName = [
    baseClasses,
    variantClasses[variant] ?? variantClasses.neutral,
    sizeClasses[size] ?? sizeClasses.sm,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={combinedClassName} {...rest}>
      {dot && (
        <span
          className="h-1.5 w-1.5 rounded-full bg-current shrink-0"
          aria-hidden="true"
        />
      )}
      {children}
    </span>
  );
};

export interface ChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: BadgeVariant;
  size?: BadgeSize;
  pressed?: boolean;
  selected?: boolean;
  count?: number | string;
  tone?: "default" | "ok" | "warn";
  onRemove?: () => void;
}

export const Chip = React.forwardRef<HTMLButtonElement, ChipProps>(
  (
    {
      size = "md",
      pressed,
      selected,
      count,
      onRemove,
      type = "button",
      disabled = false,
      className = "",
      children,
      onClick,
      onKeyDown,
      ...rest
    },
    ref
  ) => {
    const isPressed = Boolean(pressed ?? selected ?? false);

    const baseClasses =
      "inline-flex items-center font-medium rounded border transition-colors cursor-pointer select-none focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed";

    const stateClasses = isPressed
      ? "border-[var(--ua-accent)] bg-[var(--ua-accent)] text-[var(--ua-accent-fg)]"
      : "border-[var(--ua-border)] bg-[var(--ua-surface)] text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]";

    const sizeClasses: Record<BadgeSize, string> = {
      sm: "text-xs px-2 py-0.5 gap-1",
      md: "text-sm px-2.5 py-1.5 gap-1.5",
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
      onKeyDown?.(e);
      if (e.defaultPrevented) return;
      if ((e.key === "Enter" || e.key === " ") && !disabled) {
        e.preventDefault();
        onClick?.(e as unknown as React.MouseEvent<HTMLButtonElement>);
      } else if ((e.key === "Backspace" || e.key === "Delete") && onRemove && !disabled) {
        e.preventDefault();
        onRemove();
      }
    };

    // Branch A: Standard interactive chip without onRemove
    if (!onRemove) {
      const combinedClassName = [
        baseClasses,
        stateClasses,
        sizeClasses[size] ?? sizeClasses.md,
        className,
      ]
        .filter(Boolean)
        .join(" ");

      return (
        <button
          ref={ref}
          type={type}
          aria-pressed={isPressed}
          disabled={disabled}
          className={combinedClassName}
          onClick={onClick}
          onKeyDown={handleKeyDown}
          {...rest}
        >
          <span>{children}</span>
          {count !== undefined && (
            <span className="tabular-nums" aria-hidden="true">
              ({count})
            </span>
          )}
        </button>
      );
    }

    // Branch B: Removable chip with accessible sibling buttons inside wrapper
    const wrapperClasses = [
      "inline-flex items-center rounded border transition-colors select-none font-medium",
      stateClasses,
      className,
    ]
      .filter(Boolean)
      .join(" ");

    const mainButtonClasses = [
      "inline-flex items-center cursor-pointer bg-transparent border-0 text-inherit font-inherit disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:rounded-l",
      size === "sm" ? "text-xs pl-2 pr-1 py-0.5 gap-1" : "text-sm pl-2.5 pr-1.5 py-1 gap-1.5",
    ]
      .filter(Boolean)
      .join(" ");

    const removeButtonClasses = [
      "inline-flex items-center justify-center cursor-pointer bg-transparent border-0 text-inherit font-inherit hover:opacity-75 focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:rounded-r disabled:opacity-50 disabled:cursor-not-allowed min-w-6 min-h-6",
      size === "sm" ? "text-xs px-1.5 py-0.5" : "text-sm px-2 py-1",
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <span className={wrapperClasses}>
        <button
          ref={ref}
          type={type}
          aria-pressed={isPressed}
          disabled={disabled}
          className={mainButtonClasses}
          onClick={onClick}
          onKeyDown={handleKeyDown}
          {...rest}
        >
          <span>{children}</span>
          {count !== undefined && (
            <span className="tabular-nums ml-1" aria-hidden="true">
              ({count})
            </span>
          )}
        </button>
        <button
          type="button"
          aria-label="Remove"
          disabled={disabled}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className={removeButtonClasses}
        >
          <span aria-hidden="true">×</span>
        </button>
      </span>
    );
  }
);

Chip.displayName = "Chip";
