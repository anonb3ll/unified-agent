import { renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useIdentity, useSignedInIdentity } from "../../../src/app/lib/identity";
import { mockFetch } from "../support/fetchMock";
import { whoamiFixture } from "../support/fixtures";
import { testQueryClient } from "../support/render";

afterEach(() => vi.unstubAllGlobals());

function wrap() {
  const client = testQueryClient();
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useSignedInIdentity", () => {
  it("is undefined while whoami loads, then the label", async () => {
    mockFetch({ "GET /api/whoami": { body: whoamiFixture() } });
    const { result } = renderHook(() => useSignedInIdentity(), { wrapper: wrap() });
    expect(result.current).toBeUndefined();
    await waitFor(() => expect(result.current).toBe("operator"));
  });

  it("is null when signed out or the session has no agent", async () => {
    mockFetch({ "GET /api/whoami": { status: 401 } });
    const signedOut = renderHook(() => useSignedInIdentity(), { wrapper: wrap() });
    await waitFor(() => expect(signedOut.result.current).toBeNull());
    mockFetch({ "GET /api/whoami": { body: whoamiFixture({ agent: null }) } });
    const noAgent = renderHook(() => useSignedInIdentity(), { wrapper: wrap() });
    await waitFor(() => expect(noAgent.result.current).toBeNull());
  });

  it("useIdentity tells loading from failed and offers a retry (finding 7)", async () => {
    let fail = true;
    mockFetch({ "GET /api/whoami": () => (fail ? { status: 503 } : { body: whoamiFixture() }) });
    const { result } = renderHook(() => useIdentity(), { wrapper: wrap() });
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("failed"), { timeout: 12_000 });
    expect(result.current.identity).toBeUndefined();
    fail = false;
    result.current.retry();
    await waitFor(() => expect(result.current).toMatchObject({ status: "ready", identity: "operator" }));
  });

  it("a 403 is failed, not loading forever (finding 7)", async () => {
    mockFetch({ "GET /api/whoami": { status: 403 } });
    const { result } = renderHook(() => useIdentity(), { wrapper: wrap() });
    await waitFor(() => expect(result.current.status).toBe("failed"));
  });
});
