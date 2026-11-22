import { useId, type InputHTMLAttributes, type ReactNode, type Ref } from "react";
import { cx, focusRing } from "./cx";

type BoxProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type"> & {
  label: ReactNode;
  description?: ReactNode;
  ref?: Ref<HTMLInputElement>;
};

/** A checkbox with its own label. Controlled (`checked`) or uncontrolled (`defaultChecked`). */
export function Checkbox({ label, description, id, className, ref, ...props }: BoxProps) {
  const generated = useId();
  const inputId = id ?? `checkbox-${generated}`;
  const descId = description ? `${inputId}-desc` : undefined;
  return (
    <div className={cx("flex items-start gap-2", className)}>
      <input
        ref={ref}
        id={inputId}
        type="checkbox"
        aria-describedby={descId}
        className={cx(
          "mt-0.5 size-4 shrink-0 cursor-pointer accent-[var(--ua-accent)] max-md:size-6",
          focusRing,
        )}
        {...props}
      />
      <div className="flex min-h-5 flex-col max-md:min-h-11 max-md:justify-center">
        <label htmlFor={inputId} className="cursor-pointer text-sm text-[var(--ua-fg)]">
          {label}
        </label>
        {description && (
          <p id={descId} className="m-0 text-xs text-[var(--ua-muted)]">
            {description}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * An on/off setting that takes effect at once. A native checkbox with
 * `role="switch"`, so it is focusable, labelled and keyboard operable.
 */
export function Switch({ label, description, id, className, ref, ...props }: BoxProps) {
  const generated = useId();
  const inputId = id ?? `switch-${generated}`;
  const descId = description ? `${inputId}-desc` : undefined;
  return (
    <div className={cx("flex items-start gap-2.5", className)}>
      <span className="relative mt-0.5 inline-flex shrink-0 items-center max-md:mt-2.5">
        <input
          ref={ref}
          id={inputId}
          type="checkbox"
          role="switch"
          aria-describedby={descId}
          className="peer absolute inset-0 z-10 m-0 size-full cursor-pointer opacity-0"
          {...props}
        />
        <span
          aria-hidden="true"
          className={cx(
            "inline-block h-5 w-9 rounded-full border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] transition-colors",
            "peer-checked:border-[var(--ua-accent)] peer-checked:bg-[var(--ua-accent)]",
            "peer-focus-visible:outline-3 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[var(--ua-accent)]",
            "peer-disabled:opacity-50",
          )}
        />
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-0.5 top-0.5 size-4 rounded-full bg-[var(--ua-border-strong)] transition-transform peer-checked:translate-x-4 peer-checked:bg-[var(--ua-accent-fg)]"
        />
      </span>
      <div className="flex flex-col max-md:min-h-11 max-md:justify-center">
        <label htmlFor={inputId} className="cursor-pointer text-sm text-[var(--ua-fg)]">
          {label}
        </label>
        {description && (
          <p id={descId} className="m-0 text-xs text-[var(--ua-muted)]">
            {description}
          </p>
        )}
      </div>
    </div>
  );
}

export type RadioOption = { value: string; label: ReactNode; description?: ReactNode; disabled?: boolean };

export type RadioGroupProps = {
  legend: ReactNode;
  name?: string;
  options: readonly RadioOption[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  required?: boolean;
  /** Visually hide the legend (it stays the group's accessible name). */
  legendHidden?: boolean;
  orientation?: "vertical" | "horizontal";
  className?: string;
};

/** One choice from a short list, all options visible. Native radios in a fieldset. */
export function RadioGroup({
  legend,
  name,
  options,
  value,
  defaultValue,
  onChange,
  required,
  legendHidden,
  orientation = "vertical",
  className,
}: RadioGroupProps) {
  const generated = useId();
  const groupName = name ?? `radio-${generated}`;
  return (
    <fieldset className={cx("m-0 min-w-0 border-0 p-0", className)}>
      <legend className={cx("mb-1 p-0 text-sm font-medium text-[var(--ua-fg)]", legendHidden && "sr-only")}>
        {legend}
      </legend>
      <div className={cx("flex gap-2", orientation === "vertical" ? "flex-col" : "flex-row flex-wrap gap-x-4")}>
        {options.map((option) => {
          const optionId = `${groupName}-${option.value}`;
          const descId = option.description ? `${optionId}-desc` : undefined;
          return (
            <div key={option.value} className="flex items-start gap-2">
              <input
                id={optionId}
                type="radio"
                name={groupName}
                value={option.value}
                required={required}
                disabled={option.disabled}
                aria-describedby={descId}
                {...(value !== undefined
                  ? { checked: value === option.value }
                  : { defaultChecked: defaultValue === option.value })}
                onChange={(event) => {
                  if (event.target.checked) onChange?.(option.value);
                }}
                className={cx("mt-0.5 size-4 shrink-0 cursor-pointer accent-[var(--ua-accent)] max-md:size-6", focusRing)}
              />
              <div className="flex flex-col max-md:min-h-11 max-md:justify-center">
                <label htmlFor={optionId} className="cursor-pointer text-sm text-[var(--ua-fg)]">
                  {option.label}
                </label>
                {option.description && (
                  <p id={descId} className="m-0 text-xs text-[var(--ua-muted)]">
                    {option.description}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </fieldset>
  );
}
