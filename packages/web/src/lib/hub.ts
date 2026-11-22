/**
 * Typed hub surface for the Control Center SPA: a re-export shim over
 * `src/api/<domain>.ts` (task #1575), so existing `./hub` imports keep
 * working. Add new fetchers, query keys and types to the domain module.
 */
export * from "./api/approvals";
export * from "./api/attention";
export * from "./api/auth";
export * from "./api/canvas";
export * from "./api/capabilities";
export * from "./api/channels";
export * from "./api/cloud";
export * from "./api/connectors";
export * from "./api/crew";
export * from "./api/devices";
export * from "./api/dispatch";
export * from "./api/evidence";
export * from "./api/executor";
export * from "./api/flow";
export * from "./api/goals";
export * from "./api/graph";
export * from "./api/health";
export { fetchCsrfToken, postCsrfJson } from "./api/http";
export * from "./api/nodes";
export * from "./api/overview";
export * from "./api/plans";
export * from "./api/platform";
export * from "./api/proposals";
export * from "./api/provenance";
export * from "./api/queue";
export * from "./api/quota";
export * from "./api/sessions";
export * from "./api/tasks";
export * from "./api/telemetry";
