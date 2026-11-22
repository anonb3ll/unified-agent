import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignInView } from "../src/lib/auth/SignInView";
import { ThemeProvider } from "../src/lib/theme/ThemeProvider";

class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners = new Map<string, Set<(e: Event) => void>>();
  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }
  addEventListener(t: string, l: (e: Event) => void) {
    if (!this.listeners.has(t)) this.listeners.set(t, new Set());
    this.listeners.get(t)!.add(l);
  }
  removeEventListener() {}
  close() {}
}

function renderWithClient(ui: React.ReactElement, client?: QueryClient) {
  const queryClient =
    client ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0 },
      },
    });
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  );
}

describe("Authentication & Session Recovery", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  describe("SignInView component", () => {
    it("offers Sign in with WorkOS only when the hub advertises it", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL): Promise<unknown> => {
          if (String(input).includes("/api/session/options")) {
            return { ok: true, status: 200, json: async () => ({ workos: true }) };
          }
          return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
        }),
      );

      renderWithClient(<SignInView />);

      const link = await screen.findByRole("link", { name: "Sign in with WorkOS" });
      expect(link).toHaveAttribute("href", "/api/session/workos/login");
      // The bearer form stays available alongside it.
      expect(screen.getByLabelText("Agent Bearer Token")).toBeInTheDocument();
    });

    it("keeps the WorkOS link hidden when the hub does not offer it or the probe fails", async () => {
      for (const reply of [
        { ok: true, status: 200, json: async () => ({ workos: false }) },
        { ok: false, status: 404, json: async () => ({}) },
        { ok: true, status: 200, json: async () => ({ workos: "yes" }) },
      ]) {
        const fetchMock = vi.fn(async () => reply);
        vi.stubGlobal("fetch", fetchMock);
        const { unmount } = renderWithClient(<SignInView />);
        await waitFor(() => expect(fetchMock).toHaveBeenCalled());
        expect(screen.queryByRole("link", { name: "Sign in with WorkOS" })).toBeNull();
        unmount();
      }
    });

    it("renders accessible sign in form and respects WCAG 2.2 AA", () => {
      renderWithClient(<SignInView />);

      expect(screen.getByRole("heading", { name: "Sign In" })).toBeInTheDocument();
      const tokenInput = screen.getByLabelText("Agent Bearer Token");
      expect(tokenInput).toBeInTheDocument();
      expect(tokenInput).toHaveAttribute("type", "password");
      expect(tokenInput).toBeRequired();

      const submitButton = screen.getByRole("button", { name: "Sign In" });
      expect(submitButton).toBeDisabled();
    });

    it("displays error message with role='alert' when authentication fails", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL): Promise<unknown> => {
          const url = String(input);
          if (url.includes("/api/session")) {
            return {
              ok: false,
              status: 401,
              json: async () => ({ error: "invalid token" }),
              text: async () => JSON.stringify({ error: "invalid token" }),
            };
          }
          return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
        }),
      );

      renderWithClient(<SignInView />);

      const tokenInput = screen.getByLabelText("Agent Bearer Token");
      fireEvent.change(tokenInput, { target: { value: "invalid-token" } });

      const submitButton = screen.getByRole("button", { name: "Sign In" });
      expect(submitButton).not.toBeDisabled();
      fireEvent.click(submitButton);

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("invalid token");
      expect(localStorage.getItem("token")).toBeNull();
      expect(localStorage.getItem("agent_token")).toBeNull();
      expect(sessionStorage.length).toBe(0);
    });

    it("calls onSuccess and clears input on successful authentication without persisting tokens", async () => {
      const onSuccess = vi.fn();
      let postedBody: any = null;

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes("/api/session")) {
            postedBody = JSON.parse(String(init?.body ?? "{}"));
            return {
              ok: true,
              status: 200,
              json: async () => ({ agent: "Desktop_agent3", csrf: "token-csrf" }),
              text: async () => JSON.stringify({ agent: "Desktop_agent3", csrf: "token-csrf" }),
            };
          }
          if (url.includes("/api/whoami")) {
            // Sign-in confirms the cookie survived before reporting success.
            return {
              ok: true,
              status: 200,
              json: async () => ({ agent: "Desktop_agent3", csrf: "token-csrf" }),
              text: async () => "",
            };
          }
          return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
        }),
      );

      renderWithClient(<SignInView onSuccess={onSuccess} />);

      const tokenInput = screen.getByLabelText("Agent Bearer Token") as HTMLInputElement;
      fireEvent.change(tokenInput, { target: { value: "test-token" } });
      fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

      await waitFor(() => {
        expect(onSuccess).toHaveBeenCalledTimes(1);
      });

      expect(postedBody).toEqual({ token: "test-token" });
      expect(tokenInput.value).toBe("");
      expect(localStorage.getItem("token")).toBeNull();
      expect(localStorage.getItem("agent_token")).toBeNull();
      expect(sessionStorage.length).toBe(0);
    });

    it("explains a session the browser did not keep instead of flashing back to the form", async () => {
      // The hub mints a session but the follow-up whoami is still 401: a stale or
      // forwarded address, blocked cookies, or a proxy dropping Set-Cookie.
      const onSuccess = vi.fn();

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL): Promise<unknown> => {
          if (String(input).includes("/api/session")) {
            return {
              ok: true,
              status: 200,
              json: async () => ({ agent: "Desktop_agent3" }),
              text: async () => "",
            };
          }
          return {
            ok: false,
            status: 401,
            json: async () => ({ error: "unauthorized" }),
            text: async () => "unauthorized",
          };
        }),
      );

      renderWithClient(<SignInView onSuccess={onSuccess} />);

      const tokenInput = screen.getByLabelText("Agent Bearer Token") as HTMLInputElement;
      fireEvent.change(tokenInput, { target: { value: "test-token" } });
      fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(/did not keep the session cookie/i);
      expect(alert.textContent).toContain(window.location.origin);
      expect(onSuccess).not.toHaveBeenCalled();
      // The token stays put so retrying after fixing the address needs no retyping.
      expect(tokenInput.value).toBe("test-token");
      expect(sessionStorage.length).toBe(0);
    });

    it("does not blame the cookie when the hub is failing", async () => {
      // A 5xx from a proxy or an unhealthy hub is not cookie rejection: a
      // fulfilled fetch only proves a response arrived (Devin review, PR #788).
      const onSuccess = vi.fn();

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL): Promise<unknown> => {
          if (String(input).includes("/api/session")) {
            return {
              ok: true,
              status: 200,
              json: async () => ({ agent: "Desktop_agent3" }),
              text: async () => "",
            };
          }
          return {
            ok: false,
            status: 502,
            json: async () => ({ error: "bad gateway" }),
            text: async () => "bad gateway",
          };
        }),
      );

      renderWithClient(<SignInView onSuccess={onSuccess} />);

      fireEvent.change(screen.getByLabelText("Agent Bearer Token"), {
        target: { value: "test-token" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(/could not confirm the session \(HTTP 502\)/i);
      // It reports the hub fault instead of claiming the cookie was not kept.
      expect(alert.textContent).not.toMatch(/did not keep the session cookie/i);
      expect(onSuccess).not.toHaveBeenCalled();
    });

    it("blames an unreachable hub rather than the cookie", async () => {
      const onSuccess = vi.fn();

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL): Promise<unknown> => {
          if (String(input).includes("/api/session")) {
            return {
              ok: true,
              status: 200,
              json: async () => ({ agent: "Desktop_agent3" }),
              text: async () => "",
            };
          }
          throw new TypeError("network down");
        }),
      );

      renderWithClient(<SignInView onSuccess={onSuccess} />);

      fireEvent.change(screen.getByLabelText("Agent Bearer Token"), {
        target: { value: "test-token" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(/could not be reached to confirm the session/i);
      expect(onSuccess).not.toHaveBeenCalled();
    });
  });
});
