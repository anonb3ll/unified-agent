import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { buttonClassName, type ButtonVariant } from "./Button";
import { cx } from "./cx";

export type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "children"> & {
  /** Required: an icon has no text, so this is the button's accessible name. */
  "aria-label": string;
  icon: ReactNode;
  variant?: ButtonVariant;
  size?: "sm" | "md";
  ref?: Ref<HTMLButtonElement>;
};

/** A square button holding one icon. The label doubles as the tooltip. */
export function IconButton({
  icon,
  variant = "ghost",
  size = "md",
  className,
  type = "button",
  title,
  ref,
  ...rest
}: IconButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      title={title ?? rest["aria-label"]}
      className={cx(
        buttonClassName(variant, "sm"),
        "!p-0 shrink-0",
        size === "sm" ? "size-7" : "size-8",
        "max-md:size-11",
        className,
      )}
      {...rest}
    >
      <span aria-hidden="true" className="inline-flex">
        {icon}
      </span>
    </button>
  );
}
