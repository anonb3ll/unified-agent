import { useEffect, useId, useRef, type FormEvent, type ReactNode, type RefObject } from "react";
import { Button } from "./Button";
import { cx } from "./cx";

export type DialogProps = {
  open: boolean;
  /** Called on Escape, the close button, a backdrop click, or Cancel. */
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Action row (buttons), right aligned. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Element to focus when the dialog opens; defaults to the first focusable element. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Close when the dimmed backdrop is clicked. */
  dismissOnBackdrop?: boolean;
  /** Internal: render as a side drawer. Use `Drawer` instead. */
  placement?: "center" | "end";
  /**
   * Makes the body and the action row one `<form>` (with `noValidate`: the
   * caller validates and shows errors through `Field`): a `type="submit"`
   * button in the footer submits it, and so does Enter in a single-line
   * field (native implicit submission). The handler gets the submit event.
   */
  onSubmit?: (event: FormEvent<HTMLFormElement>) => void;
  /**
   * With `onSubmit`: whether Enter in a single-line field submits the form
   * (native implicit submission). Default true. When false, only activating
   * the submit button submits.
   */
  submitOnEnter?: boolean;
  className?: string;
};

function isOpen(dialog: HTMLDialogElement): boolean {
  return dialog.hasAttribute("open");
}

function show(dialog: HTMLDialogElement) {
  if (typeof dialog.showModal === "function") {
    try {
      dialog.showModal();
      return;
    } catch {
      // Not connected yet or already open in a non-modal way: fall through.
    }
  }
  dialog.setAttribute("open", "");
}

function hide(dialog: HTMLDialogElement) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

const SIZE = { sm: "max-w-sm", md: "max-w-lg", lg: "max-w-3xl" } as const;

/**
 * Modal dialog on the native `<dialog>` element: `showModal()` makes the rest
 * of the page inert (the focus trap), Escape closes, and focus returns to the
 * element that opened it. The dialog is named by its title and described by
 * its description.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  initialFocusRef,
  dismissOnBackdrop = true,
  placement = "center",
  onSubmit,
  submitOnEnter = true,
  className,
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !isOpen(dialog)) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      show(dialog);
      const target =
        initialFocusRef?.current ??
        dialog.querySelector<HTMLElement>(
          "[autofocus], input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), [data-dialog-body] button:not([disabled]), [data-dialog-footer] button:not([disabled])",
        );
      target?.focus();
    } else if (!open && isOpen(dialog)) {
      hide(dialog);
      returnFocus.current?.focus();
      returnFocus.current = null;
    }
  }, [open, initialFocusRef]);

  // Unmounting while open still hands focus back.
  useEffect(
    () => () => {
      returnFocus.current?.focus();
    },
    [],
  );

  const placementClass =
    placement === "end"
      ? "m-0 ml-auto h-dvh max-h-dvh w-full max-w-xl rounded-none border-y-0 border-r-0"
      : cx("m-auto w-[calc(100%-2rem)] rounded-lg", SIZE[size]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        onCloseRef.current();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !event.defaultPrevented) {
          event.preventDefault();
          event.stopPropagation();
          onCloseRef.current();
        }
      }}
      onClick={(event) => {
        if (dismissOnBackdrop && event.target === event.currentTarget) onCloseRef.current();
      }}
      className={cx(
        "border border-[var(--ua-border-strong)] bg-[var(--ua-raised)] p-0 text-[var(--ua-fg)] shadow-2xl",
        "backdrop:bg-[color-mix(in_srgb,var(--ua-bg)_75%,transparent)]",
        placementClass,
        className,
      )}
    >
      {open && (
        <div className="flex max-h-[inherit] flex-col">
          <div className="flex items-start gap-3 border-b border-[var(--ua-border)] px-5 py-4">
            <div className="min-w-0 flex-1">
              <h2 id={titleId} className="m-0 text-base font-semibold">
                {title}
              </h2>
              {description && (
                <p id={descriptionId} className="m-0 mt-1 text-sm text-[var(--ua-muted)]">
                  {description}
                </p>
              )}
            </div>
            <Button variant="ghost" size="sm" aria-label="Close dialog" onClick={() => onCloseRef.current()}>
              <span aria-hidden="true">✕</span>
            </Button>
          </div>
          {(() => {
            const content = (
              <>
                {children !== undefined && (
                  <div data-dialog-body className="min-h-0 flex-1 overflow-auto px-5 py-4 text-sm">
                    {children}
                  </div>
                )}
                {footer && (
                  <div data-dialog-footer className="flex flex-wrap justify-end gap-2 border-t border-[var(--ua-border)] px-5 py-3">
                    {footer}
                  </div>
                )}
              </>
            );
            return onSubmit ? (
              <form
                // The page shows its own field errors (Field's error wiring);
                // the browser's required-field popup would pre-empt them.
                // Fields keep `required` (announced as required).
                noValidate
                aria-labelledby={titleId}
                className="flex min-h-0 flex-1 flex-col"
                onSubmit={onSubmit}
                onKeyDown={(event) => {
                  if (submitOnEnter || event.key !== "Enter") return;
                  const target = event.target as HTMLElement;
                  // Block implicit submission from a field; buttons, links and
                  // multi-line text keep their own Enter.
                  if (target instanceof HTMLInputElement) event.preventDefault();
                }}
              >
                {content}
              </form>
            ) : (
              content
            );
          })()}
        </div>
      )}
    </dialog>
  );
}

export type DrawerProps = Omit<DialogProps, "placement" | "size">;

/** A dialog that slides in from the end edge: detail of a row while the list stays put. */
export function Drawer(props: DrawerProps) {
  return <Dialog {...props} placement="end" />;
}

export type ConfirmDialogProps = {
  open: boolean;
  /** What is about to happen, as a question: "Delete this memory note?" */
  title: ReactNode;
  /** The object the action applies to, named so the person can check it. */
  objectName?: ReactNode;
  /** What happens afterwards, in plain words: "It can be restored for 30 days." */
  consequence: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** `danger` for destructive or irreversible actions. */
  variant?: "default" | "danger";
  /** Shows the confirm button as busy and blocks a second press. */
  busy?: boolean;
  /** Disable the confirm button (and Enter) until the caller's fields are valid. */
  confirmDisabled?: boolean;
  /**
   * Whether Enter in a single-line field confirms. Default: true, except for
   * `danger`, where the person must activate the confirm button itself.
   */
  submitOnEnter?: boolean;
  /**
   * Form controls between the consequence and the actions (a reason field,
   * a picker). The dialog is a form: the confirm button submits it, and
   * Enter in a single-line field confirms.
   */
  children?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
};

/**
 * The only way to confirm an action (never `window.confirm`). It names the
 * object and the consequence. A danger confirmation opens with focus on
 * Cancel, so a stray Enter does not destroy anything.
 */
export function ConfirmDialog({
  open,
  title,
  objectName,
  consequence,
  confirmLabel,
  cancelLabel = "Cancel",
  variant = "default",
  busy = false,
  confirmDisabled = false,
  submitOnEnter,
  children,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      dismissOnBackdrop={!busy}
      // With fields, focus the first one (the default); otherwise the safe button.
      initialFocusRef={children ? undefined : variant === "danger" ? cancelRef : confirmRef}
      onSubmit={(event) => {
        event.preventDefault();
        if (!busy && !confirmDisabled) onConfirm();
      }}
      submitOnEnter={submitOnEnter ?? variant !== "danger"}
      footer={
        <>
          <Button ref={cancelRef} variant="secondary" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            ref={confirmRef}
            type="submit"
            variant={variant === "danger" ? "danger" : "primary"}
            loading={busy}
            disabled={confirmDisabled}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {objectName && <p className="m-0 mb-2 font-medium break-words">{objectName}</p>}
      <p className="m-0 text-[var(--ua-muted)]">{consequence}</p>
      {children && <div className="mt-4 flex flex-col gap-3">{children}</div>}
    </Dialog>
  );
}
