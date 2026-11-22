import { createContext, useContext, useId, type ReactNode } from "react";
import { cx } from "./cx";

type FieldContextValue = {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
  required: boolean;
};

const FieldContext = createContext<FieldContextValue | null>(null);

export type FieldProps = {
  label: ReactNode;
  /** Short help shown under the control. */
  hint?: ReactNode;
  /** What is wrong and how to fix it. Marks the control invalid. */
  error?: ReactNode;
  required?: boolean;
  /** Visually hide the label (it stays the accessible name). */
  labelHidden?: boolean;
  /** Control id; generated when omitted. */
  id?: string;
  className?: string;
  children: ReactNode;
};

/**
 * Label, hint and error around one control. The control (`Input`,
 * `Textarea`, `Select`, `Combobox`) reads the wiring from context:
 * `id` for the label's `htmlFor`, `aria-describedby` for hint and error,
 * `aria-invalid` and `required`.
 */
export function Field({ label, hint, error, required = false, labelHidden, id, className, children }: FieldProps) {
  const generated = useId();
  const controlId = id ?? `field-${generated}`;
  const hintId = hint ? `${controlId}-hint` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("flex flex-col gap-1", className)}>
      <label
        htmlFor={controlId}
        className={cx("text-sm font-medium text-[var(--ua-fg)]", labelHidden && "sr-only")}
      >
        {label}
        {required && (
          <span aria-hidden="true" className="ml-0.5 text-[var(--ua-danger)]">
            *
          </span>
        )}
      </label>
      <FieldContext.Provider value={{ id: controlId, describedBy, invalid: Boolean(error), required }}>
        {children}
      </FieldContext.Provider>
      {hint && (
        <p id={hintId} className="m-0 text-xs text-[var(--ua-muted)]">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="m-0 text-xs font-medium text-[var(--ua-danger)]">
          {error}
        </p>
      )}
    </div>
  );
}

type ControlAria = {
  id?: string;
  required?: boolean;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false" | "grammar" | "spelling";
};

/** Merge a control's own props with the enclosing `Field`'s wiring. Own props win. */
export function useFieldControl<P extends ControlAria>(props: P): P {
  const field = useContext(FieldContext);
  if (!field) return props;
  const describedBy = [field.describedBy, props["aria-describedby"]].filter(Boolean).join(" ") || undefined;
  return {
    ...props,
    id: props.id ?? field.id,
    required: props.required ?? (field.required || undefined),
    "aria-describedby": describedBy,
    "aria-invalid": props["aria-invalid"] ?? (field.invalid || undefined),
  };
}
