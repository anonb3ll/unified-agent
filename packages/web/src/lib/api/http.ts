/**
 * Shared same-origin HTTP helpers: CSRF token, error detail and CSRF JSON POST.
 * Re-exported from `hub.ts` (task #1575).
 */

/** CSRF from GET /api/whoami — never via the mutating api() path (no recursion). */
export async function fetchCsrfToken(signal?: AbortSignal): Promise<string> {
  const response = await fetch("/api/whoami", {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`whoami HTTP ${response.status}`);
  }
  const me = (await response.json()) as {
    csrf?: string;
    csrf_token?: string;
  };
  const token = me.csrf || me.csrf_token;
  if (!token) {
    throw new Error("whoami response missing csrf");
  }
  return token;
}

export async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { error?: string };
    if (body && typeof body.error === "string") return body.error;
  } catch {
    // non-JSON body
  }
  try {
    return (await response.text()).slice(0, 200);
  } catch {
    return "";
  }
}
/**
 * POST JSON to a Control Center /api/action/* route with CSRF.
 * On 403 csrf errors, refresh the token once and retry (legacy submitAction).
 */
export async function postCsrfJson(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  let csrf = await fetchCsrfToken(signal);

  const attempt = async (token: string, retried: boolean): Promise<unknown> => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-ua-csrf": token,
      },
      body: JSON.stringify(body),
      signal,
    });

    if (response.status === 403 && !retried) {
      const detail = await readErrorDetail(response);
      if (/csrf/i.test(detail)) {
        csrf = await fetchCsrfToken(signal);
        return attempt(csrf, true);
      }
    }

    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(detail || `${path} HTTP ${response.status}`);
    }

    // 202 Accepted (dispatch) still returns JSON
    const textBody = await response.text();
    if (!textBody) return {};
    try {
      return JSON.parse(textBody) as unknown;
    } catch {
      return { raw: textBody };
    }
  };

  return attempt(csrf, false);
}
