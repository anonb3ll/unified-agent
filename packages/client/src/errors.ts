/** Errors raised by the hub client. Never include bearer tokens in messages. */

export class UnifiedAgentClientError extends Error {
  readonly code: string;

  constructor(code: string, message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "UnifiedAgentClientError";
    this.code = code;
  }
}

export class HubAuthError extends UnifiedAgentClientError {
  readonly status: number;

  constructor(status: number, message: string) {
    super("hub_auth", message);
    this.name = "HubAuthError";
    this.status = status;
  }
}

export class HubRpcError extends UnifiedAgentClientError {
  readonly rpc: unknown;

  constructor(message: string, rpc: unknown) {
    super("hub_rpc", message);
    this.name = "HubRpcError";
    this.rpc = rpc;
  }
}

export class HubTransportError extends UnifiedAgentClientError {
  /** HTTP status when the failure came from a response; unset for network errors. */
  readonly status?: number;

  constructor(
    message: string,
    options?: { cause?: unknown; status?: number },
  ) {
    super("hub_transport", message, options);
    this.name = "HubTransportError";
    if (options?.status !== undefined) {
      this.status = options.status;
    }
  }
}

/** Typed response failed Zod (or equivalent) validation — fail closed. */
export class HubValidationError extends UnifiedAgentClientError {
  constructor(message: string, options?: { cause?: unknown }) {
    super("hub_validation", message, options);
    this.name = "HubValidationError";
  }
}

/**
 * The hub's `initialize` response negotiated a protocol version this SDK
 * does not recognize. Fail closed rather than proceed with a session whose
 * wire contract this SDK cannot reason about — see
 * `KNOWN_HANDSHAKE_PROTOCOL_VERSIONS` in `hub.ts` and
 * docs/compatibility-matrix.md.
 */
export class HubNegotiationError extends UnifiedAgentClientError {
  readonly negotiatedProtocolVersion: string;

  constructor(negotiatedProtocolVersion: string, message?: string) {
    super(
      "hub_negotiation",
      message ?? `hub negotiated an unrecognized protocol version: ${negotiatedProtocolVersion}`,
    );
    this.name = "HubNegotiationError";
    this.negotiatedProtocolVersion = negotiatedProtocolVersion;
  }
}
