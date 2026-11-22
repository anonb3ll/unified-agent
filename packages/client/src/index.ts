export { UnifiedAgentClient } from "./client.js";
export {
  CapabilityCatalog,
  ControlCenterClient,
  NodeSessionCancelOutcome,
  NodeSessionOutcome,
  NodesPayload,
  PlatformManifest,
  QueueHealth,
  Run,
  RunsPage,
  SessionsListPayload,
} from "./control-center.js";
export type {
  NodeSessionCancelOutcome as NodeSessionCancelOutcomeType,
  NodeSessionOutcome as NodeSessionOutcomeType,
  NodesPayload as NodesPayloadType,
  Run as RunType,
  RunListQuery,
  RunsPage as RunsPageType,
  SessionsListPayload as SessionsListPayloadType,
} from "./control-center.js";
export type {
  ApprovalPending,
  ApprovalResolveParams,
  DelegateParams,
  DeliveryReceipt,
  DeliveryReceiptLease,
  DeliveryReceiptTransition,
  EventLogEntry,
  EventLogQueryParams,
  EventLogQueryResult,
  EventLogVerifyResult,
  EvidenceReportParams,
  EvidenceReportResult,
  ExecutorAuthProof,
  McpHealthTelemetry,
  MemorySaveParams,
  MemorySearchHit,
  MemorySearchParams,
  ProfileStatus,
  CapabilityCatalogResult,
  CapabilityCatalogParams,
  SystemDoctorReport,
  TaskAddParams,
  TaskClaimParams,
  TaskListParams,
  TaskUpdateParams,
  UnifiedAgentClientOptions,
  VaultQueryParams,
  VaultQueryResult,
  VaultReadParams,
  VaultReadResult,
  VaultSearchParams,
  VaultSearchResult,
} from "./client.js";
export type {
  ControlCenterClientOptions,
  CapabilityCatalog as CapabilityCatalogType,
  PlatformManifest as PlatformManifestType,
  QueueHealth as QueueHealthType,
  QueueHealthCounts,
  QueueHealthTask,
  PlatformObjectRef,
  ApprovalRequest,
  ApprovalResolveRequest,
  EvidenceCreateRequest,
  TaskCreateRequest,
  TaskUpdateRequest,
  WorkProductSubmitRequest,
} from "./control-center.js";

export {
  HubTransport,
  KNOWN_HANDSHAKE_PROTOCOL_VERSIONS,
  MCP_PROTOCOL_VERSION,
  decodeRpc,
  decodeSseFrames,
  decodeSseRpc,
  unwrapToolResult,
} from "./hub.js";
export type {
  FetchLike,
  HubClientCapabilities,
  HubProgressEvent,
  HubServerCapabilities,
  HubTransportOptions,
} from "./hub.js";
export type { CallToolOptions } from "./hub.js";

export {
  HubAuthError,
  HubNegotiationError,
  HubRpcError,
  HubTransportError,
  HubValidationError,
  UnifiedAgentClientError,
} from "./errors.js";

// Re-export protocol shapes so consumers can depend on one package for calls + types.
export {
  Approval,
  MemoryRecord,
  Task,
  TaskClaimResult,
  TaskClaimWait,
  TaskSafeWait,
  isTaskSafeWait,
  ToolInvocation,
  DispatchRequest,
  PolicyDecision,
} from "@unified-agent/protocol";
export type {
  Approval as ApprovalType,
  MemoryRecord as MemoryRecordType,
  Task as TaskType,
  TaskClaimResult as TaskClaimResultType,
  TaskClaimWait as TaskClaimWaitType,
  TaskSafeWait as TaskSafeWaitType,
  ToolInvocation as ToolInvocationType,
  DispatchRequest as DispatchRequestType,
  PolicyDecision as PolicyDecisionType,
} from "@unified-agent/protocol";
