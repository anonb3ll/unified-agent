import React from "react";

export type CardVariant = "surface" | "sunken" | "default" | "interactive";
export type CardPadding = "none" | "sm" | "md" | "lg";

export interface CardProps extends React.HTMLAttributes<HTMLElement> {
  as?: "article" | "section" | "div" | "li";
  variant?: CardVariant;
  padding?: CardPadding;
  interactive?: boolean;
  onClick?: (e?: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => void;
}

export interface CardHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  bordered?: boolean;
}

export interface CardTitleProps extends React.HTMLAttributes<HTMLHeadingElement> {
  as?: "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
}

export interface CardDescriptionProps extends React.HTMLAttributes<HTMLParagraphElement> {}

export interface CardBodyProps extends React.HTMLAttributes<HTMLDivElement> {}

export interface CardFooterProps extends React.HTMLAttributes<HTMLDivElement> {
  bordered?: boolean;
}

const CardRoot = React.forwardRef<HTMLElement, CardProps>(
  (
    {
      as = "div",
      variant = "default",
      padding = "md",
      interactive,
      className = "",
      children,
      onClick,
      onKeyDown,
      role,
      tabIndex,
      ...rest
    },
    ref
  ) => {
    const Tag = as as any;
    const isInteractive = Boolean(onClick || interactive || variant === "interactive");

    const baseClasses =
      "rounded-lg border border-[var(--ua-border)] text-[var(--ua-fg)] flex flex-col";

    const variantClasses: Record<CardVariant, string> = {
      default: "bg-[var(--ua-surface)]",
      surface: "bg-[var(--ua-surface)]",
      sunken: "bg-[var(--ua-bg)]",
      interactive:
        "bg-[var(--ua-surface)] cursor-pointer hover:border-[var(--ua-accent)] hover:bg-[var(--ua-hover)] transition-colors focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2",
    };

    const interactiveClasses = isInteractive
      ? "cursor-pointer hover:border-[var(--ua-accent)] hover:bg-[var(--ua-hover)] transition-colors focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2"
      : "";

    const paddingClasses: Record<CardPadding, string> = {
      none: "p-0",
      sm: "p-2.5",
      md: "p-4",
      lg: "p-6",
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
      onKeyDown?.(e);
      if (e.defaultPrevented) return;
      if (e.target !== e.currentTarget) return;
      if ((e.key === "Enter" || e.key === " ") && isInteractive) {
        e.preventDefault();
        onClick?.(e);
      }
    };

    const combinedClassName = [
      baseClasses,
      variantClasses[variant] ?? variantClasses.default,
      interactiveClasses,
      paddingClasses[padding],
      className,
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <Tag
        ref={ref}
        role={isInteractive ? role ?? "button" : role}
        tabIndex={isInteractive ? tabIndex ?? 0 : tabIndex}
        className={combinedClassName}
        onClick={onClick}
        onKeyDown={handleKeyDown}
        {...rest}
      >
        {children}
      </Tag>
    );
  }
);

CardRoot.displayName = "Card";

const CardHeader: React.FC<CardHeaderProps> = ({
  bordered = false,
  className = "",
  children,
  ...rest
}) => {
  const combined = [
    "flex flex-wrap items-center justify-between gap-2",
    bordered && "border-b border-[var(--ua-border)] pb-3 mb-3",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={combined} {...rest}>
      {children}
    </div>
  );
};
CardHeader.displayName = "Card.Header";

const CardTitle: React.FC<CardTitleProps> = ({
  as = "h3",
  className = "",
  children,
  ...rest
}) => {
  const Heading = as;
  const combined = ["font-semibold text-[var(--ua-fg)] m-0 text-base", className]
    .filter(Boolean)
    .join(" ");

  return (
    <Heading className={combined} {...rest}>
      {children}
    </Heading>
  );
};
CardTitle.displayName = "Card.Title";

const CardDescription: React.FC<CardDescriptionProps> = ({
  className = "",
  children,
  ...rest
}) => {
  const combined = ["text-sm text-[var(--ua-muted)] m-0 mt-0.5", className]
    .filter(Boolean)
    .join(" ");

  return (
    <p className={combined} {...rest}>
      {children}
    </p>
  );
};
CardDescription.displayName = "Card.Description";

const CardBody: React.FC<CardBodyProps> = ({
  className = "",
  children,
  ...rest
}) => {
  const combined = ["flex-1 min-w-0", className].filter(Boolean).join(" ");

  return (
    <div className={combined} {...rest}>
      {children}
    </div>
  );
};
CardBody.displayName = "Card.Body";

const CardFooter: React.FC<CardFooterProps> = ({
  bordered = false,
  className = "",
  children,
  ...rest
}) => {
  const combined = [
    "flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--ua-muted)]",
    bordered && "border-t border-[var(--ua-border)] pt-3 mt-3",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={combined} {...rest}>
      {children}
    </div>
  );
};
CardFooter.displayName = "Card.Footer";

export const Card = Object.assign(CardRoot, {
  Header: CardHeader,
  Title: CardTitle,
  Description: CardDescription,
  Body: CardBody,
  Footer: CardFooter,
});
