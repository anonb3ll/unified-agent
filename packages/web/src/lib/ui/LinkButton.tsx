import type { ReactNode } from "react";
import { buttonClassName, type ButtonSize, type ButtonVariant } from "./Button";
import { UiLink, type UiLinkProps } from "./link";

export type LinkButtonProps = UiLinkProps & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  leftIcon?: ReactNode;
  rightIcon?: ReactNode;
};

/**
 * A link that looks like a button. Use it whenever the action is "go to a
 * page" (a real `<a href>`, so middle-click and copy link work); use `Button`
 * for actions that change something.
 */
export function LinkButton({
  variant = "secondary",
  size = "md",
  leftIcon,
  rightIcon,
  className = "",
  children,
  ...rest
}: LinkButtonProps) {
  return (
    <UiLink className={buttonClassName(variant, size, `no-underline ${className}`)} {...rest}>
      {leftIcon && (
        <span className="inline-flex shrink-0" aria-hidden="true">
          {leftIcon}
        </span>
      )}
      <span>{children}</span>
      {rightIcon && (
        <span className="inline-flex shrink-0" aria-hidden="true">
          {rightIcon}
        </span>
      )}
    </UiLink>
  );
}
