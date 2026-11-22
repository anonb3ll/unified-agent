import type { InputHTMLAttributes, Ref, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { controlClass, cx } from "./cx";
import { useFieldControl } from "./Field";

export type InputProps = InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement> };

/** Single-line text entry. Controlled (`value`) or uncontrolled (`defaultValue`). */
export function Input({ className, ref, ...props }: InputProps) {
  const merged = useFieldControl(props);
  return <input ref={ref} className={cx(controlClass, className)} {...merged} />;
}

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & { ref?: Ref<HTMLTextAreaElement> };

/** Multi-line text entry. */
export function Textarea({ className, ref, rows = 4, ...props }: TextareaProps) {
  const merged = useFieldControl(props);
  return <textarea ref={ref} rows={rows} className={cx(controlClass, "leading-normal", className)} {...merged} />;
}

export type SelectOption = { value: string; label: string; disabled?: boolean };

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & {
  options?: readonly SelectOption[];
  /** Adds a first, empty option with this text (for "any" or "choose"). */
  placeholder?: string;
  ref?: Ref<HTMLSelectElement>;
};

/** Native select: the most robust picker for a short, fixed list. */
export function Select({ className, options, placeholder, children, ref, ...props }: SelectProps) {
  const merged = useFieldControl(props);
  return (
    <select ref={ref} className={cx(controlClass, "pr-8", className)} {...merged}>
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options?.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>
          {o.label}
        </option>
      ))}
      {children}
    </select>
  );
}
