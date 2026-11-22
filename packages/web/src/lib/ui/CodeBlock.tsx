import { useState } from "react";
import { Button } from "./Button";
import { cx } from "./cx";

export type CodeBlockProps = {
  code: string;
  /** Names the block for assistive tech and shows above it: "Command to run". */
  label: string;
  language?: string;
  copyable?: boolean;
  className?: string;
};

/**
 * Read-only code or command output. The block is a focusable, labelled
 * region so a keyboard user can scroll long lines; Copy puts the text on
 * the clipboard and says so.
 */
export function CodeBlock({ code, label, language, copyable = true, className }: CodeBlockProps) {
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
  };
  return (
    <figure className={cx("m-0 overflow-hidden rounded-md border border-[var(--ua-border)] bg-[var(--ua-sunken)]", className)}>
      <figcaption className="flex items-center gap-2 border-b border-[var(--ua-border)] bg-[var(--ua-surface)] px-3 py-1 text-xs text-[var(--ua-muted)]">
        <span className="flex-1">
          {label}
          {language && <span className="ml-2 font-mono">{language}</span>}
        </span>
        <span role="status" className="text-[var(--ua-fg)]">
          {copied === "copied" ? "Copied" : copied === "failed" ? "Copy failed: select the text instead" : ""}
        </span>
        {copyable && (
          <Button size="sm" variant="ghost" onClick={copy} aria-label={`Copy ${label}`}>
            Copy
          </Button>
        )}
      </figcaption>
      <pre tabIndex={0} aria-label={label} className="m-0 overflow-x-auto p-3 font-mono text-xs leading-relaxed text-[var(--ua-fg)] focus-visible:outline-3 focus-visible:-outline-offset-2 focus-visible:outline-[var(--ua-accent)]">
        <code>{code}</code>
      </pre>
    </figure>
  );
}
