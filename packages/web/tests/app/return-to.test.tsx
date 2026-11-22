import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { rememberReturnPath, takeReturnPath } from "../../src/app/lib/returnTo";
import { findPageHeading, renderRoute } from "./support/render";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("return path through WorkOS sign-in (finding 11)", () => {
  it("restores a remembered v2 path once when the hub lands on /ui/", () => {
    rememberReturnPath("/ui/agents/hosts?tab=x#y");
    expect(takeReturnPath("/ui/")).toBe("/ui/agents/hosts?tab=x#y");
    expect(takeReturnPath("/ui/")).toBeNull();
  });

  it("does not hijack a page that is not the sign-in landing", () => {
    rememberReturnPath("/ui/agents/hosts");
    expect(takeReturnPath("/ui/work")).toBeNull();
    expect(takeReturnPath("/ui/")).toBeNull();
  });

  it.each(["https://evil.example/ui/home", "//evil.example/ui/home", "/ui/", "/ui/index.html", "/other/home", "/ui/\\evil", "javascript:alert(1)", "/ui//evil.example"])(
    "never stores or restores %s",
    (path) => {
      rememberReturnPath(path);
      expect(takeReturnPath("/ui/")).toBeNull();
    },
  );

  it("expires after ten minutes", () => {
    vi.useFakeTimers();
    rememberReturnPath("/ui/agents/hosts");
    vi.advanceTimersByTime(11 * 60_000);
    expect(takeReturnPath("/ui/")).toBeNull();
    vi.useRealTimers();
  });

  it("the sign-in gate remembers the deep link when WorkOS sign-in is clicked", async () => {
    renderRoute("/agents/hosts", {
      hub: {
        "GET /api/whoami": { status: 401, body: { error: "unauthorized" } },
        "GET /api/session/options": { body: { workos: true } },
      },
    });
    const link = await screen.findByRole("link", { name: /Sign in with WorkOS/i }, { timeout: 12_000 });
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    await waitFor(() => expect(takeReturnPath("/ui/")).toBe("/ui/agents/hosts"));
  });

  it("a token sign-in clears the remembered path", async () => {
    let signedIn = false;
    rememberReturnPath("/ui/agents/hosts");
    renderRoute("/work", {
      hub: {
        "GET /api/whoami": () => (signedIn ? { body: { agent: "operator", csrf: "c", hub_version: "1", api_version: 1 } } : { status: 401 }),
        "POST /api/session": () => {
          signedIn = true;
          return { body: { agent: "operator", csrf: "c" } };
        },
        "GET /api/session/options": { body: { workos: false } },
      },
    });
    fireEvent.change(await screen.findByLabelText(/Bearer Token/i, {}, { timeout: 12_000 }), { target: { value: "t" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
    await findPageHeading("Work");
    expect(takeReturnPath("/ui/")).toBeNull();
  });
});
