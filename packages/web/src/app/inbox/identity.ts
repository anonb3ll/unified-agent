import { useQuery } from "@tanstack/react-query";
import { fetchWhoami, WHOAMI_QUERY_KEY, type WhoamiPayload } from "../../lib/api/auth";
import { useRootQueryClient } from "../lib/epoch";

/** The shell's whoami read (shared cache), or undefined while unknown. */
export function useWhoami(): WhoamiPayload | undefined {
  // whoami is not owner data: it lives in the root client (spec §6, owner epoch).
  const whoami = useQuery({ queryKey: WHOAMI_QUERY_KEY, queryFn: ({ signal }) => fetchWhoami(signal), retry: false }, useRootQueryClient());
  return whoami.data ?? undefined;
}

/** The signed-in identity, or null while unknown. */
export function useIdentity(): string | null {
  return useWhoami()?.agent ?? null;
}

/** A fresh client id for one composed message; kept with the draft so a retry is not a second message. */
export function newMessageId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
