/**
 * Sign-in, whoami, session cookies and approver passkeys.
 * Re-exported from `hub.ts` (task #1575).
 */
import { readErrorDetail } from "./http";

/** Outcome of confirming that a minted session cookie is actually being sent. */
export type SessionConfirmation =
  | { outcome: "confirmed" }
  | { outcome: "rejected" }
  | { outcome: "unavailable"; status: number }
  | { outcome: "unreachable" };

/**
 * Confirm the session cookie survives a fresh request after POST /api/session.
 *
 * A 200 from the sign-in POST only means the hub minted a session. A browser
 * that does not send the cookie back — a stale or forwarded address (the cookie
 * belongs to a different origin), blocked cookies, or a proxy stripping
 * `Set-Cookie` — leaves the app unauthenticated even though sign-in "worked",
 * which the shell renders as a silent flash back to the form.
 *
 * `rejected` is reserved for authentication statuses (401/403): only those mean
 * the cookie was refused. A fulfilled fetch proves a response arrived, not that
 * the session was denied, so a 5xx reverse-proxy or hub error is `unavailable` —
 * blaming the cookie for an unhealthy hub sends the operator down the wrong path.
 *
 * Distinct from `fetchCsrfToken`: this only needs the HTTP status, so a hub
 * running without configured tokens (no `csrf` in the payload) still confirms.
 */
export async function confirmSession(): Promise<SessionConfirmation> {
  try {
    const response = await fetch("/api/whoami", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.ok) return { outcome: "confirmed" };
    if (response.status === 401 || response.status === 403) {
      return { outcome: "rejected" };
    }
    return { outcome: "unavailable", status: response.status };
  } catch {
    return { outcome: "unreachable" };
  }
}

/** Session identity and hub/API compatibility from GET /api/whoami. */
export type WhoamiPayload = {
  hub_version: string;
  api_version: number;
  agent?: string | null;
  via?: string | null;
  csrf?: string | null;
  enforce_auth?: boolean;
  tokens_configured?: boolean;
  control_enabled?: boolean;
  control_disabled_reason?: string | null;
};

/** Typed HTTP authentication error carrying status code. */
export class HttpAuthError extends Error {
  readonly status: number;

  constructor(status: number, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = "HttpAuthError";
    this.status = status;
  }
}

/** TanStack Query key for GET /api/whoami. */
export const WHOAMI_QUERY_KEY = ["whoami"] as const;

export async function fetchWhoami(
  signal?: AbortSignal,
): Promise<WhoamiPayload> {
  const response = await fetch("/api/whoami", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new HttpAuthError(response.status, `whoami HTTP ${response.status}`);
  }
  return (await response.json()) as WhoamiPayload;
}

type SessionCreateResponse = {
  agent: string;
  csrf: string;
};

/** Sign-in methods the hub offers, from the public GET /api/session/options. */
type SessionOptions = {
  workos: boolean;
};

/** Where "Sign in with WorkOS" sends the browser (a full-page navigation). */
export const WORKOS_SIGN_IN_PATH = "/api/session/workos/login";

/**
 * Ask the hub which sign-in methods it offers. Public and unauthenticated.
 *
 * Any failure (an older hub without the route, a network error, a body that
 * is not the expected shape) reads as "bearer only", so the token form always
 * stays usable.
 */
export async function getSessionOptions(
  signal?: AbortSignal,
): Promise<SessionOptions> {
  try {
    const response = await fetch("/api/session/options", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) return { workos: false };
    const payload = (await response.json()) as { workos?: unknown } | null;
    return { workos: payload?.workos === true };
  } catch {
    return { workos: false };
  }
}

/** Trade a bearer token for an HttpOnly session cookie via POST /api/session. */
export async function postSession(
  token: string,
  signal?: AbortSignal,
): Promise<SessionCreateResponse> {
  const response = await fetch("/api/session", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ token }),
    signal,
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new HttpAuthError(
      response.status,
      detail || `Sign in failed (HTTP ${response.status})`,
    );
  }
  return (await response.json()) as SessionCreateResponse;
}

/** One approver passkey registered on the hub (task #1322). Public metadata only. */
export type ApproverPasskey = {
  credential_id: string;
  login: string;
  rp_id: string;
  alg: number;
  label?: string | null;
  registered_by?: string | null;
  created_at?: number | null;
  last_used_at?: number | null;
};

export type ApproverPasskeysPayload = {
  passkeys: ApproverPasskey[];
  approver_logins: string[];
  rp_id?: string | null;
  origin?: string | null;
  this_device_login?: string | null;
  peer_is_loopback?: boolean;
  passphrase_fallback?: boolean;
  error?: string;
};
