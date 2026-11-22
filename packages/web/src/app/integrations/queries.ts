/**
 * The integrations reads as TanStack queries. Reads with events on the hub
 * stream refetch when one arrives; every read also polls calmly so a change
 * made elsewhere appears. A refetch only replaces read data: forms keep their
 * own state, so an open form is never reset by one.
 *
 * Reads live in the owner epoch's QueryClient (the one the routed tree is
 * rendered under), and a stream event refetches only while that epoch is
 * live: an event that arrives after the owner changed touches nothing.
 */
import { useQuery } from "@tanstack/react-query";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import { useStreamInvalidation } from "../../lib/polling";
import {
  CALM_POLL_MS,
  fetchConnectorInventory,
  fetchNotifyStatus,
  fetchOAuthConnection,
  fetchSlackStatus,
  fetchZulipStatus,
  INTEGRATION_KEYS,
  IntegrationReadError,
  type OAuthProvider,
} from "./api";

/** Do not retry what retrying cannot fix: sign-in, permission, a missing route. */
function retry(failureCount: number, error: Error): boolean {
  if (error instanceof IntegrationReadError && error.status !== null && [401, 403, 404].includes(error.status)) return false;
  return failureCount < 1;
}

const COMMON = { refetchInterval: CALM_POLL_MS, retry } as const;

/** Refetch `queryKey` in this epoch's client, only while the epoch is live. */
function refetcher(epoch: OwnerEpoch, queryKey: readonly unknown[]) {
  return () => void epoch.run(() => epoch.queryClient.invalidateQueries({ queryKey }));
}

export function useConnectorInventory() {
  const epoch = useEpoch();
  useStreamInvalidation(refetcher(epoch, INTEGRATION_KEYS.inventory), {
    channels: ["horizon2.*"],
  });
  return useQuery({ queryKey: INTEGRATION_KEYS.inventory, queryFn: ({ signal }) => fetchConnectorInventory(signal), ...COMMON });
}

export function useOAuthConnection(provider: OAuthProvider) {
  const epoch = useEpoch();
  useStreamInvalidation(refetcher(epoch, INTEGRATION_KEYS.oauth(provider)), {
    channels: ["connector.oauth.*"],
  });
  return useQuery({
    queryKey: INTEGRATION_KEYS.oauth(provider),
    queryFn: ({ signal }) => fetchOAuthConnection(provider, signal),
    ...COMMON,
  });
}

export function useSlackStatus() {
  const epoch = useEpoch();
  useStreamInvalidation(refetcher(epoch, INTEGRATION_KEYS.slack), { channels: ["slack.*"] });
  return useQuery({ queryKey: INTEGRATION_KEYS.slack, queryFn: ({ signal }) => fetchSlackStatus(signal), ...COMMON });
}

export function useZulipStatus() {
  const epoch = useEpoch();
  useStreamInvalidation(refetcher(epoch, INTEGRATION_KEYS.zulip), { channels: ["zulip.*"] });
  return useQuery({ queryKey: INTEGRATION_KEYS.zulip, queryFn: ({ signal }) => fetchZulipStatus(signal), ...COMMON });
}

/** Notification delivery writes no stream event; the calm poll brings changes. */
export function useNotifyStatus() {
  return useQuery({ queryKey: INTEGRATION_KEYS.notify, queryFn: ({ signal }) => fetchNotifyStatus(signal), ...COMMON });
}
