import { useEffect, useState, type FormEvent } from "react";
import {
  confirmSession,
  getSessionOptions,
  postSession,
  HttpAuthError,
  WORKOS_SIGN_IN_PATH,
  type SessionConfirmation,
} from "../hub";
import { ThemeToggle } from "../ui/ThemeToggle";

type SignInViewProps = {
  onSuccess?: () => void;
};

/**
 * Explain why a session the hub accepted is not usable in this browser.
 *
 * Shown instead of silently re-rendering the form, so the operator can tell the
 * likely causes apart: the page is on an address the cookie does not belong to,
 * cookies are blocked or stripped, or the hub is unreachable or failing.
 * `rejected` is the only outcome that blames the cookie — see `confirmSession`.
 */
function confirmationFailure(
  result: Exclude<SessionConfirmation, { outcome: "confirmed" }>,
): string {
  if (result.outcome === "rejected") {
    return `The hub accepted your token, but this browser did not keep the session cookie, so you are still signed out. This page is on ${window.location.origin}. Open the hub on its own address — a stale, forwarded, or different address sends the cookie to a different origin — and make sure cookies are allowed for it.`;
  }
  if (result.outcome === "unavailable") {
    return `The hub accepted your token, but it could not confirm the session (HTTP ${result.status}). That is the hub failing the request rather than refusing the cookie — check the hub's health before retrying the token.`;
  }
  return "The hub accepted your token, but it could not be reached to confirm the session. Check that this page's address is the hub you are signing in to.";
}

/**
 * Accessible authentication view displayed when the SPA receives a 401
 * or has an expired session.
 *
 * Enforces WCAG 2.2 AA accessibility:
 * - Form controls explicitly labeled
 * - Error messages with role="alert" and aria-live="polite"
 * - High contrast and focus rings
 * - Strict security: tokens are submitted directly to POST /api/session
 *   and NEVER persisted to localStorage or sessionStorage.
 * - When the hub advertises it (GET /api/session/options), a "Sign in with
 *   WorkOS" link starts the AuthKit round trip; the hub sets the same session
 *   cookie on the way back, so nothing about the session lives in script.
 */
export function SignInView({ onSuccess }: SignInViewProps) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [workosAvailable, setWorkosAvailable] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void getSessionOptions(controller.signal).then((options) => {
      if (!controller.signal.aborted) setWorkosAvailable(options.workos);
    });
    return () => controller.abort();
  }, []);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const cleanToken = token.trim();
    if (!cleanToken) {
      setError("Please enter a token.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      await postSession(cleanToken);

      // A 200 from POST /api/session only means the hub minted a session. If the
      // browser never sends that cookie back, the shell's whoami query stays 401
      // and re-renders this form — which the operator experienced as a flash with
      // no message. Confirm before handing control back, and say why when it fails.
      const confirmation = await confirmSession();
      if (confirmation.outcome !== "confirmed") {
        setError(confirmationFailure(confirmation));
        return;
      }

      setToken("");
      onSuccess?.();
    } catch (err) {
      if (err instanceof HttpAuthError) {
        setError(err.message || "Invalid token or authentication failed.");
      } else if (err instanceof Error) {
        setError(err.message || "Sign in failed. Please try again.");
      } else {
        setError("An unexpected error occurred during sign in.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col justify-between bg-[var(--ua-bg)] text-[var(--ua-fg)] p-4">
      <header className="flex justify-end p-2">
        <ThemeToggle />
      </header>

      <main className="mx-auto w-full max-w-md">
        <div className="rounded-lg border border-[var(--ua-border-strong)] bg-[var(--ua-surface)] p-6 shadow-sm">
          <div className="mb-6 text-center">
            <p className="m-0 text-xs font-semibold uppercase tracking-[0.14em] text-[var(--ua-muted)]">
              Unified Agent
            </p>
            <h1 className="mt-1 text-2xl font-bold">Sign In</h1>
            <p className="mt-2 text-sm text-[var(--ua-muted)]">
              Your session has expired or requires authentication. Enter your agent token to continue.
            </p>
          </div>

          {error && (
            <div
              role="alert"
              aria-live="polite"
              className="mb-4 rounded border border-[var(--ua-danger)] bg-[var(--ua-danger-surface)] p-3 text-sm text-[var(--ua-danger)] font-medium"
            >
              {error}
            </div>
          )}

          {workosAvailable && (
            <div className="mb-4 space-y-4">
              <a
                href={WORKOS_SIGN_IN_PATH}
                className="block w-full rounded bg-[var(--ua-accent)] px-4 py-2.5 text-center text-sm font-semibold text-[var(--ua-accent-fg)] no-underline shadow hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)] transition-opacity"
              >
                Sign in with WorkOS
              </a>
              <p className="m-0 text-center text-xs uppercase tracking-[0.08em] text-[var(--ua-muted)]">
                or use an agent token
              </p>
            </div>
          )}

          <form onSubmit={handleSubmit} noValidate={false} className="space-y-4">
            <div>
              <label
                htmlFor="agent-token-input"
                className="block text-xs font-medium uppercase tracking-[0.08em] text-[var(--ua-muted)] mb-1"
              >
                Agent Bearer Token
              </label>
              <input
                id="agent-token-input"
                name="token"
                type="password"
                autoComplete="current-password"
                required
                value={token}
                onChange={(e) => setToken(e.target.value)}
                disabled={submitting}
                placeholder="Enter bearer token"
                className="w-full rounded border border-[var(--ua-border-strong)] bg-[var(--ua-bg)] px-3 py-2 text-sm text-[var(--ua-fg)] placeholder-[var(--ua-muted)] focus:border-[var(--ua-accent)] focus:outline-none focus:ring-1 focus:ring-[var(--ua-accent)] disabled:opacity-50"
              />
            </div>

            <button
              type="submit"
              disabled={submitting || !token.trim()}
              className="w-full rounded bg-[var(--ua-accent)] px-4 py-2.5 text-sm font-semibold text-[var(--ua-accent-fg)] shadow hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer transition-opacity"
            >
              {submitting ? "Signing in…" : "Sign In"}
            </button>
          </form>
        </div>
      </main>

      <footer className="py-4 text-center text-xs text-[var(--ua-muted)]">
        Control Center · Cookie session authentication
      </footer>
    </div>
  );
}
