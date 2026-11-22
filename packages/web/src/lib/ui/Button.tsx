import React from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  isLoading?: boolean;
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
}

const baseClasses =
  "inline-flex items-center justify-center font-medium rounded transition-colors cursor-pointer select-none disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2";

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    "border border-[var(--ua-accent)] bg-[var(--ua-accent)] text-[var(--ua-accent-fg)] hover:opacity-90 active:opacity-100",
  secondary:
    "border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]",
  danger:
    "border border-[var(--ua-danger)] bg-[var(--ua-surface)] text-[var(--ua-danger)] hover:bg-[var(--ua-danger-surface)]",
  ghost:
    "border border-transparent bg-transparent text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "px-2 py-1 text-xs gap-1.5 min-h-[1.75rem]",
  md: "px-2.5 py-1.5 text-sm gap-2 min-h-[2.25rem]",
  lg: "px-3.5 py-2 text-base gap-2.5 min-h-[2.75rem]",
};

/** The button look as a class string, shared with `LinkButton` and `IconButton`. */
export function buttonClassName(
  variant: ButtonVariant = "secondary",
  size: ButtonSize = "md",
  className = "",
): string {
  return [baseClasses, variantClasses[variant] ?? variantClasses.secondary, sizeClasses[size] ?? sizeClasses.md, className]
    .filter(Boolean)
    .join(" ");
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "secondary",
      size = "md",
      loading = false,
      isLoading = false,
      leftIcon,
      rightIcon,
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
    const isBusy = Boolean(loading || isLoading);
    const isDisabled = Boolean(disabled || isBusy);

    // No keydown handling: a native <button> already turns Enter and Space
    // into a click, and a type="submit" button submits its form. Intercepting
    // the keys (preventDefault + a synthetic onClick) cancelled form submission.

    const combinedClassName = buttonClassName(variant, size, className);

    return (
      <button
        ref={ref}
        type={type}
        disabled={isDisabled}
        aria-busy={isBusy ? "true" : undefined}
        className={combinedClassName}
        onClick={onClick}
        onKeyDown={onKeyDown}
        {...rest}
      >
        {isBusy ? (
          <svg
            className="animate-spin -ml-0.5 mr-1.5 h-3.5 w-3.5 text-current"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
            />
          </svg>
        ) : (
          leftIcon && <span className="inline-flex shrink-0" aria-hidden="true">{leftIcon}</span>
        )}
        <span>{children}</span>
        {!isBusy && rightIcon && (
          <span className="inline-flex shrink-0" aria-hidden="true">{rightIcon}</span>
        )}
      </button>
    );
  }
);

Button.displayName = "Button";
