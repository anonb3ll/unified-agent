import { Component, type ErrorInfo, type ReactNode } from "react";
// Direct import: the src/lib/ui barrel would pull the graph library into the entry chunk.
import { Button } from "../lib/ui/Button";

type Props = {
  children: ReactNode;
  /** Defaults to a full page reload. */
  onReload?: () => void;
};

type State = { error: Error | null };

/**
 * Wraps the lazily loaded app in `src/main.tsx`. A chunk that fails to load
 * (a deploy replaced it, the network dropped) or a render error outside any
 * route boundary would otherwise blank the page; this offers Reload instead.
 */
export class EntryBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Control Center could not load", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const reload = this.props.onReload ?? (() => window.location.reload());
    return (
      <main id="main-content" className="mx-auto flex min-h-dvh max-w-xl flex-col justify-center gap-4 px-4 text-[var(--ua-fg)]">
        <h1 className="m-0 text-xl font-semibold">The Control Center could not load</h1>
        <p className="m-0 text-sm text-[var(--ua-muted)]">
          Reload the page to try again. If it keeps failing, the hub may be restarting after an update; wait a minute and
          reload.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={reload}>
            Reload
          </Button>
        </div>
        <details className="text-xs text-[var(--ua-fg)]">
          <summary className="cursor-pointer">Technical detail</summary>
          <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{`${error.name}: ${error.message}`}</pre>
        </details>
      </main>
    );
  }
}
