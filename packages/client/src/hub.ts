/**
 * Streamable-HTTP MCP transport for the Unified Agent hub.
 *
 * Mirrors ``unified_agent.executor.daemon.HubClient``: initialize → session id →
 * tools/call, with SSE or JSON result decoding. Policy, approval, and sandbox
 * stay on the hub — this module only speaks JSON-RPC over HTTP.
 *
 * Session lifecycle (task #939 — see examples/node-worker.mjs for a full
 * long-lived consumer):
 * - **Single-flight**: concurrent callers (``ensureSession()`` directly, or
 *   indirectly via ``callTool``/``listTools``) share one in-flight
 *   ``initialize`` — never open a duplicate session.
 * - **Retry**: expired sessions (HTTP 404) re-handshake once, then retry the
 *   same call. Transient transport failures retry only when the call is
 *   marked retryable (explicit option, or arguments carry
 *   ``idempotency_key``). Mutations without an idempotency key are never
 *   blind-retried. Auth and JSON-RPC tool errors never retry.
 * - **Timeout**: every request is bounded by ``requestTimeoutMs`` (default
 *   30s; 0 disables it) so a hung connection cannot stall a caller forever
 *   even without an explicit ``AbortSignal``. A timeout surfaces as
 *   ``HubTransportError`` with no ``status`` (the same retryability bucket as
 *   a network failure) — distinct from a caller's own cancellation, which
 *   still propagates as the caller's abort reason unwrapped.
 * - **Close**: ``close()`` is best-effort (a failed DELETE never throws) and
 *   always resets local state, so a client can be reused immediately after —
 *   the next call just re-handshakes.
 *
 * Protocol negotiation (task #938):
 * - Requests ``MCP_PROTOCOL_VERSION`` on ``initialize`` and records whatever
 *   the hub actually negotiates back — never assumes the request was honored.
 * - Fails closed (``HubNegotiationError``) if the hub returns a protocol
 *   version this SDK does not recognize as a member of
 *   ``KNOWN_HANDSHAKE_PROTOCOL_VERSIONS``.
 * - Captures the hub's advertised ``capabilities`` from the ``initialize``
 *   result (tools/resources/prompts/logging/experimental) and exposes them
 *   typed via ``serverCapabilities``.
 * - Declares this client's own capabilities (``roots``, ``elicitation``) when
 *   configured via ``HubTransportOptions.capabilities`` — see the caveat on
 *   ``HubClientCapabilities.elicitation`` below before relying on it.
 * - Surfaces ``notifications/progress`` frames via ``CallToolOptions.onProgress``
 *   when a call is made with a progress token.
 */

import {
  HubAuthError,
  HubNegotiationError,
  HubRpcError,
  HubTransportError,
} from "./errors.js";

/**
 * Every protocol revision reachable via the classic ``initialize`` handshake
 * this transport speaks, oldest to newest. Verified 2026-09-11 against the
 * installed hub's own dependency source
 * (``mcp_types.version.HANDSHAKE_PROTOCOL_VERSIONS`` and
 * ``mcp.server.runner._negotiate_initialize``): the hub echoes back whichever
 * of these the client requests, and substitutes its own newest handshake
 * revision for anything else — it never rejects ``initialize`` outright over
 * version choice. A response outside this set is a genuine contract break,
 * not a normal negotiation outcome, and is treated as such (see
 * ``HubNegotiationError``).
 */
export const KNOWN_HANDSHAKE_PROTOCOL_VERSIONS = [
  "2024-11-05",
  "2025-03-26",
  "2025-06-18",
  "2025-11-25",
] as const;

/**
 * Newest protocol revision reachable via the classic ``initialize`` handshake
 * this transport implements.
 *
 * Deliberately **not** ``2026-07-28``, even though that is the hub's reported
 * ``LATEST_PROTOCOL_VERSION`` / operator-doc "target": ``2026-07-28`` replaces
 * ``initialize`` with a stateless per-request envelope, a different wire
 * transport this SDK does not speak. The hub decides a connection's protocol
 * era from the *shape* of the very first request it receives
 * (``mcp.server.runner.serve_dual_era_loop``): a classic ``initialize``
 * request permanently opens a legacy-era connection that cannot reach
 * ``2026-07-28`` no matter what ``protocolVersion`` string is sent, and on a
 * connection that *did* open at ``2026-07-28`` a later ``initialize`` is
 * rejected outright with ``UNSUPPORTED_PROTOCOL_VERSION``. Reaching
 * ``2026-07-28`` requires a per-request-envelope transport implementation,
 * not a version-string change — see docs/compatibility-matrix.md.
 */
export const MCP_PROTOCOL_VERSION = "2025-11-25";

/**
 * Capabilities this client declares on ``initialize``. Purely a wire
 * declaration to the hub — see the ``elicitation`` note for what it actually
 * buys you today.
 */
export type HubClientCapabilities = {
  /**
   * Declares that this client can answer a server-initiated
   * ``elicitation/create`` request.
   *
   * This transport does not yet *implement* receiving or answering one —
   * doing so needs a channel for server-initiated requests, which a single
   * request/response HTTP exchange per tool call does not provide (tracked
   * for task #940, which builds the actual approval client). Declaring this
   * today is safe but inert: the hub's elicitation path already falls back
   * to parking the request in the ``/ui`` approval queue on any client error
   * or non-response (``unified_agent.webui.routes._with_elicitation``), so a
   * client that declares support but never answers just pays one extra
   * round-trip before landing on the same fallback as declaring nothing.
   */
  elicitation?: boolean;
  /**
   * Declares support for ``roots/list``. Only set this if your application
   * layer actually answers that request — this SDK does not implement it.
   */
  roots?: boolean;
};

/**
 * Server capabilities from the ``initialize`` result, typed per the MCP
 * ``ServerCapabilities`` schema. Every field is optional and the object is
 * not a closed set (a server may advertise additional keys under
 * ``experimental``) — treat absence of a field as "unknown", not "false".
 */
export type HubServerCapabilities = {
  tools?: { listChanged?: boolean };
  resources?: { listChanged?: boolean; subscribe?: boolean };
  prompts?: { listChanged?: boolean };
  logging?: Record<string, unknown>;
  completions?: Record<string, unknown>;
  experimental?: Record<string, Record<string, unknown>>;
};

export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal | undefined;
  },
) => Promise<{
  ok: boolean;
  status: number;
  headers: { get(name: string): string | null };
  text(): Promise<string>;
}>;

export type HubTransportOptions = {
  /** Full MCP endpoint, e.g. ``http://127.0.0.1:8765/mcp``. */
  url: string;
  /** Per-agent bearer token. Never logged. */
  bearerToken: string;
  /** Optional override for tests. Defaults to global ``fetch``. */
  fetch?: FetchLike;
  /** Client name reported on initialize. */
  clientName?: string;
  /** Client version reported on initialize. */
  clientVersion?: string;
  /** Capabilities declared on ``initialize``. Defaults to none declared. */
  capabilities?: HubClientCapabilities;
  /**
   * Abort a request if the hub hasn't responded within this many
   * milliseconds. Applies to every request (``initialize`` included), not
   * just ``tools/call``. Default 30000; set 0 to disable and rely solely on
   * a caller-supplied ``AbortSignal``.
   */
  requestTimeoutMs?: number;
};

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

type JsonRpcRequest = {
  jsonrpc: "2.0";
  id?: number;
  method: string;
  params?: Record<string, unknown>;
};

type JsonRpcResponse = {
  jsonrpc?: string;
  id?: number | string | null;
  result?: unknown;
  error?: { code?: number; message?: string; data?: unknown };
};

/** One ``notifications/progress`` update for an in-flight call. */
export type HubProgressEvent = {
  progress: number;
  total: number | null;
  message: string | null;
};

export type CallToolOptions = {
  /** Abort the request and notify the hub exactly once. */
  signal?: AbortSignal;
  /**
   * Allow bounded transport retries for this call. Defaults to true when
   * ``arguments`` include a non-empty ``idempotency_key``; otherwise false.
   */
  retryable?: boolean;
  /** Total attempts including the first (default 2 when retryable). Cap 3. */
  maxAttempts?: number;
  /**
   * Receive ``notifications/progress`` frames for this call. Providing this
   * attaches a ``_meta.progressToken`` to the request (the request id) so the
   * hub knows to emit progress at all — most calls finish before any
   * progress would be reported and this is a no-op for them.
   */
  onProgress?: (event: HubProgressEvent) => void;
};

const TRANSIENT_HTTP = new Set([502, 503, 504]);

export class HubTransport {
  readonly url: string;
  private readonly bearerToken: string;
  private readonly fetchImpl: FetchLike;
  private readonly clientName: string;
  private readonly clientVersion: string;
  private readonly clientCapabilities: HubClientCapabilities;
  private readonly requestTimeoutMs: number;
  private sessionId: string | null = null;
  /** Negotiated protocol version from the last successful initialize. */
  private negotiatedProtocolVersion: string | null = null;
  /** Server capabilities from the last successful initialize. */
  private negotiatedServerCapabilities: HubServerCapabilities | null = null;
  private nextId = 1;
  /** Single-flight handshake so concurrent first calls share one initialize. */
  private sessionInit: Promise<string> | null = null;

  constructor(options: HubTransportOptions) {
    if (!options.url?.trim()) {
      throw new HubTransportError("hub url is required");
    }
    if (!options.bearerToken?.trim()) {
      throw new HubAuthError(0, "bearer token is required");
    }
    this.url = options.url.replace(/\/$/, "");
    this.bearerToken = options.bearerToken;
    this.fetchImpl = options.fetch ?? (globalThis.fetch as FetchLike);
    this.clientName = options.clientName ?? "@unified-agent/client";
    this.clientVersion = options.clientVersion ?? "0.2.1";
    this.clientCapabilities = options.capabilities ?? {};
    this.requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  }

  /** Current MCP session id, if initialize has completed. */
  get mcpSessionId(): string | null {
    return this.sessionId;
  }

  /** Protocol version returned by the hub on initialize, if known. */
  get protocolVersion(): string | null {
    return this.negotiatedProtocolVersion;
  }

  /** Server capabilities from the hub's initialize result, if known. */
  get serverCapabilities(): HubServerCapabilities | null {
    return this.negotiatedServerCapabilities;
  }

  /** Drop the session so the next call re-handshakes. */
  resetSession(): void {
    this.sessionId = null;
    this.negotiatedProtocolVersion = null;
    this.negotiatedServerCapabilities = null;
    this.sessionInit = null;
  }

  async close(): Promise<void> {
    if (!this.sessionId) {
      return;
    }
    // Bounded even on close: a hung DELETE must not stall a graceful shutdown
    // (see examples/node-worker.mjs) any longer than an ordinary request would.
    const { signal, cleanup } = this.boundedSignal(undefined);
    try {
      await this.fetchImpl(this.url, {
        method: "DELETE",
        headers: this.headers(),
        ...(signal ? { signal } : {}),
      });
    } catch {
      // Best-effort teardown.
    } finally {
      cleanup();
      this.sessionId = null;
      this.negotiatedProtocolVersion = null;
      this.negotiatedServerCapabilities = null;
      this.sessionInit = null;
    }
  }

  async ensureSession(): Promise<string> {
    if (this.sessionId) {
      return this.sessionId;
    }
    if (this.sessionInit) {
      return this.sessionInit;
    }
    this.sessionInit = this.handshake()
      .catch((err) => {
        this.sessionId = null;
        this.negotiatedProtocolVersion = null;
        this.negotiatedServerCapabilities = null;
        throw err;
      })
      .finally(() => {
        this.sessionInit = null;
      });
    return this.sessionInit;
  }

  private wireCapabilities(): Record<string, unknown> {
    const wire: Record<string, unknown> = {};
    if (this.clientCapabilities.elicitation) {
      wire.elicitation = {};
    }
    if (this.clientCapabilities.roots) {
      wire.roots = { listChanged: false };
    }
    return wire;
  }

  private async handshake(): Promise<string> {
    const { body, headers } = await this.post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "initialize",
      params: {
        protocolVersion: MCP_PROTOCOL_VERSION,
        capabilities: this.wireCapabilities(),
        clientInfo: { name: this.clientName, version: this.clientVersion },
      },
    });
    if (body.error) {
      const detail =
        typeof body.error.message === "string"
          ? body.error.message
          : "initialize JSON-RPC error";
      throw new HubRpcError(detail, body.error);
    }
    const result = body.result;
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new HubTransportError("hub initialize returned no result object");
    }
    const resultObj = result as { protocolVersion?: unknown; capabilities?: unknown };
    const protocolVersion = resultObj.protocolVersion;
    if (typeof protocolVersion !== "string" || !protocolVersion.trim()) {
      throw new HubTransportError(
        "hub initialize returned no protocolVersion",
      );
    }
    const negotiated = protocolVersion.trim();
    // Fail closed: a version outside the known handshake set means either a
    // hub running an incompatible dependency version or a genuine wire
    // contract break — either way, downstream capability assumptions in this
    // SDK cannot be trusted, so don't guess.
    if (!(KNOWN_HANDSHAKE_PROTOCOL_VERSIONS as readonly string[]).includes(negotiated)) {
      throw new HubNegotiationError(negotiated);
    }
    const sessionId = headers.get("mcp-session-id");
    if (!sessionId) {
      throw new HubTransportError("hub returned no mcp-session-id on initialize");
    }
    this.negotiatedProtocolVersion = negotiated;
    this.negotiatedServerCapabilities = parseServerCapabilities(resultObj.capabilities);
    this.sessionId = sessionId;
    await this.post({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    });
    return sessionId;
  }

  async listTools(): Promise<unknown> {
    await this.ensureSession();
    const { body } = await this.postWithSessionRecovery({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "tools/list",
    });
    return this.rpcResult(body);
  }

  async callTool(
    name: string,
    args: Record<string, unknown> = {},
    options: CallToolOptions = {},
  ): Promise<Record<string, unknown>> {
    const maxAttempts = resolveMaxAttempts(args, options);
    let lastError: unknown;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      if (options.signal?.aborted) {
        throw options.signal.reason instanceof Error
          ? options.signal.reason
          : new HubTransportError("request aborted");
      }
      try {
        return await this.callToolOnce(name, args, options);
      } catch (err) {
        lastError = err;
        if (
          attempt >= maxAttempts ||
          options.signal?.aborted ||
          !isRetryableTransportFailure(err)
        ) {
          throw err;
        }
      }
    }
    throw lastError instanceof Error
      ? lastError
      : new HubTransportError("hub request failed");
  }

  private async callToolOnce(
    name: string,
    args: Record<string, unknown>,
    options: CallToolOptions,
  ): Promise<Record<string, unknown>> {
    await this.ensureSession();
    const id = this.nextId++;
    const cancel = this.cancellation(id, options.signal);
    const params: Record<string, unknown> = { name, arguments: args };
    if (options.onProgress) {
      // Attach a progress token so the hub knows to emit
      // notifications/progress at all; the request id doubles as the token.
      params._meta = { progressToken: id };
    }
    try {
      const { body } = await this.postWithSessionRecovery(
        {
          jsonrpc: "2.0",
          id,
          method: "tools/call",
          params,
        },
        options.signal,
        options.onProgress,
      );
      const result = this.rpcResult(body);
      return unwrapToolResult(result);
    } finally {
      cancel();
    }
  }

  /** Send the MCP cancellation notification for an in-flight request. */
  async cancel(requestId: number, reason = "client cancelled"): Promise<void> {
    if (!Number.isInteger(requestId) || requestId < 1) {
      throw new HubTransportError("request id must be a positive integer");
    }
    await this.ensureSession();
    await this.post({
      jsonrpc: "2.0",
      method: "notifications/cancelled",
      params: { requestId, reason },
    });
  }

  private cancellation(requestId: number, signal?: AbortSignal): () => void {
    if (!signal) return () => {};
    let sent = false;
    const onAbort = () => {
      if (sent) return;
      sent = true;
      void this.cancel(requestId, "client cancelled").catch(() => undefined);
    };
    signal.addEventListener("abort", onAbort, { once: true });
    return () => signal.removeEventListener("abort", onAbort);
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${this.bearerToken}`,
      ...extra,
    };
    if (this.sessionId) {
      headers["mcp-session-id"] = this.sessionId;
    }
    return headers;
  }

  /**
   * Like {@link post}, but on HTTP 404 clear the session, re-handshake once,
   * and retry the same payload (mirrors Python HubClient). This is session
   * recovery, not a mutation blind-retry.
   *
   * Deliberately does **not** gate on the current ``this.sessionId`` before
   * attempting recovery (task #939): every caller of this method already
   * called ``ensureSession()`` first, so a session existed for *this* call
   * regardless of what another concurrent caller has since done to the
   * shared field. Gating on it let a second concurrent caller observe the
   * session already nulled by the first caller's recovery and give up
   * instead of joining it — ``ensureSession()``'s single-flight promise is
   * what actually prevents a duplicate handshake here, not this check.
   */
  private async postWithSessionRecovery(
    payload: JsonRpcRequest,
    signal?: AbortSignal,
    onProgress?: (event: HubProgressEvent) => void,
  ): Promise<{ body: JsonRpcResponse; headers: { get(name: string): string | null } }> {
    try {
      return await this.post(payload, signal, onProgress);
    } catch (err) {
      if (err instanceof HubTransportError && err.status === 404) {
        this.sessionId = null;
        this.negotiatedProtocolVersion = null;
        this.negotiatedServerCapabilities = null;
        await this.ensureSession();
        return await this.post(payload, signal, onProgress);
      }
      throw err;
    }
  }

  private async post(
    payload: JsonRpcRequest,
    signal?: AbortSignal,
    onProgress?: (event: HubProgressEvent) => void,
  ): Promise<{ body: JsonRpcResponse; headers: { get(name: string): string | null } }> {
    const { signal: effectiveSignal, cleanup } = this.boundedSignal(signal);
    try {
      return await this.postWithSignal(payload, effectiveSignal, onProgress);
    } finally {
      cleanup();
    }
  }

  /**
   * Combine a caller-supplied ``AbortSignal`` with this transport's request
   * timeout. Returns the caller's own signal unwrapped when the timeout is
   * disabled, so caller-driven cancellation behaves exactly as before this
   * existed (see ``isAbortError`` handling in {@link postWithSignal}).
   */
  private boundedSignal(callerSignal?: AbortSignal): {
    signal: AbortSignal | undefined;
    cleanup: () => void;
  } {
    if (this.requestTimeoutMs <= 0) {
      return { signal: callerSignal, cleanup: () => {} };
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort(
        new HubTransportError(`hub request timed out after ${this.requestTimeoutMs}ms`),
      );
    }, this.requestTimeoutMs);
    const forwardAbort = () => controller.abort(callerSignal?.reason);
    if (callerSignal?.aborted) {
      controller.abort(callerSignal.reason);
    } else {
      callerSignal?.addEventListener("abort", forwardAbort, { once: true });
    }
    return {
      signal: controller.signal,
      cleanup: () => {
        clearTimeout(timer);
        callerSignal?.removeEventListener("abort", forwardAbort);
      },
    };
  }

  private async postWithSignal(
    payload: JsonRpcRequest,
    signal?: AbortSignal,
    onProgress?: (event: HubProgressEvent) => void,
  ): Promise<{ body: JsonRpcResponse; headers: { get(name: string): string | null } }> {
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await this.fetchImpl(this.url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(payload),
        ...(signal ? { signal } : {}),
      });
    } catch (cause) {
      // Our own timeout sets a HubTransportError as the abort reason; surface
      // it as-is rather than wrapping it again. A caller's own AbortSignal
      // still propagates unwrapped (isAbortError), unchanged from before.
      if (cause instanceof HubTransportError) {
        throw cause;
      }
      if (isAbortError(cause)) {
        throw cause;
      }
      throw new HubTransportError("hub request failed", { cause });
    }
    if (response.status === 401 || response.status === 403) {
      throw new HubAuthError(
        response.status,
        `hub rejected credentials (HTTP ${response.status})`,
      );
    }
    if (!response.ok && response.status !== 200) {
      const text = await response.text().catch(() => "");
      throw new HubTransportError(
        `hub HTTP ${response.status}${text ? `: ${text.slice(0, 200)}` : ""}`,
        { status: response.status },
      );
    }
    const text = await response.text();
    let body: JsonRpcResponse;
    try {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("text/event-stream")) {
        const frames = decodeSseFrames(text);
        body = pickResponseFrame(frames, onProgress);
      } else {
        body = decodeRpc(response.headers, text);
      }
    } catch (cause) {
      throw new HubTransportError("hub returned malformed JSON-RPC", {
        cause,
        status: response.status,
      });
    }
    return { body, headers: response.headers };
  }

  private rpcResult(message: JsonRpcResponse): unknown {
    if (message.error) {
      const detail =
        typeof message.error.message === "string"
          ? message.error.message
          : "JSON-RPC error";
      throw new HubRpcError(detail, message.error);
    }
    return message.result;
  }
}

export function decodeRpc(
  headers: { get(name: string): string | null },
  text: string,
): JsonRpcResponse {
  const contentType = headers.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    return decodeSseRpc(text);
  }
  if (!text.trim()) {
    return {};
  }
  return JSON.parse(text) as JsonRpcResponse;
}

/** A raw SSE frame: a JSON-RPC response (has ``id``) or notification (does not). */
type JsonRpcFrame = JsonRpcResponse & { method?: string; params?: Record<string, unknown> };

/**
 * Parse every SSE frame in a response body, in order. Multi-line ``data:``
 * lines are joined with ``\\n`` (SSE spec). A streamable-HTTP response to one
 * request can carry several frames: zero or more notifications (e.g.
 * ``notifications/progress``) followed by the JSON-RPC response.
 */
export function decodeSseFrames(text: string): JsonRpcFrame[] {
  const frames: JsonRpcFrame[] = [];
  let dataLines: string[] = [];

  const flush = () => {
    if (dataLines.length === 0) {
      return;
    }
    const payload = dataLines.join("\n").trim();
    dataLines = [];
    if (!payload) {
      return;
    }
    frames.push(JSON.parse(payload) as JsonRpcFrame);
  };

  for (const line of text.split(/\r?\n/)) {
    if (line.startsWith("data:")) {
      // Preserve intentional leading space after the colon only when present.
      dataLines.push(line.slice(5).startsWith(" ") ? line.slice(6) : line.slice(5));
      continue;
    }
    if (line === "") {
      flush();
    }
  }
  flush();

  if (frames.length === 0) {
    throw new HubTransportError("hub returned an SSE response with no data frame");
  }
  return frames;
}

/**
 * Pick the JSON-RPC response out of a set of frames, routing any
 * ``notifications/progress`` frames to ``onProgress`` along the way. Prefers
 * the last frame carrying a ``result``/``error``; falls back to the last
 * frame if none do (mirrors the pre-progress-aware tie-break exactly).
 */
function pickResponseFrame(
  frames: JsonRpcFrame[],
  onProgress?: (event: HubProgressEvent) => void,
): JsonRpcResponse {
  for (const frame of frames) {
    if (frame.id === undefined && frame.method === "notifications/progress") {
      if (onProgress) {
        const params = frame.params ?? {};
        onProgress({
          progress: typeof params.progress === "number" ? params.progress : 0,
          total: typeof params.total === "number" ? params.total : null,
          message: typeof params.message === "string" ? params.message : null,
        });
      }
    }
  }
  for (let i = frames.length - 1; i >= 0; i--) {
    const frame = frames[i]!;
    if (frame.result !== undefined || frame.error !== undefined) {
      return frame;
    }
  }
  return frames[frames.length - 1]!;
}

/**
 * Parse one or more SSE frames and return the JSON-RPC response, discarding
 * any notification frames (e.g. progress) without surfacing them. Prefer
 * {@link decodeSseFrames} plus {@link CallToolOptions.onProgress} when
 * notifications matter to the caller.
 */
export function decodeSseRpc(text: string): JsonRpcResponse {
  return pickResponseFrame(decodeSseFrames(text));
}

/**
 * Loosely parse the ``capabilities`` object from an ``initialize`` result.
 * The MCP spec explicitly leaves this an open set ("any server can define
 * its own, additional capabilities") — this keeps only the known nested
 * shapes typed and passes ``logging``/``completions``/``experimental``
 * through as opaque objects rather than guessing their internal shape.
 */
function parseServerCapabilities(raw: unknown): HubServerCapabilities {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {};
  }
  const obj = raw as Record<string, unknown>;
  const out: HubServerCapabilities = {};
  for (const key of ["tools", "resources", "prompts"] as const) {
    const value = obj[key];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      out[key] = value as Record<string, unknown>;
    }
  }
  for (const key of ["logging", "completions"] as const) {
    const value = obj[key];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      out[key] = value as Record<string, unknown>;
    }
  }
  if (obj.experimental && typeof obj.experimental === "object" && !Array.isArray(obj.experimental)) {
    out.experimental = obj.experimental as Record<string, Record<string, unknown>>;
  }
  return out;
}

function toolErrorMessage(envelope: Record<string, unknown>): string {
  const content = envelope.content;
  if (Array.isArray(content)) {
    const texts: string[] = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const item = block as Record<string, unknown>;
      if (item.type === "text" && typeof item.text === "string" && item.text.trim()) {
        texts.push(item.text);
      }
    }
    if (texts.length > 0) {
      return texts.join("\n");
    }
  }
  return "hub reported a tool error";
}

export function unwrapToolResult(result: unknown): Record<string, unknown> {
  if (!result || typeof result !== "object") {
    return { result };
  }
  const envelope = result as Record<string, unknown>;
  if (envelope.isError) {
    throw new HubRpcError(toolErrorMessage(envelope), envelope);
  }
  const structured = envelope.structuredContent;
  if (structured && typeof structured === "object" && !Array.isArray(structured)) {
    return structured as Record<string, unknown>;
  }
  const content = envelope.content;
  if (Array.isArray(content)) {
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const item = block as Record<string, unknown>;
      if (item.type === "text" && typeof item.text === "string") {
        try {
          const parsed: unknown = JSON.parse(item.text);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            return parsed as Record<string, unknown>;
          }
          return { result: parsed };
        } catch {
          return { text: item.text };
        }
      }
    }
  }
  return envelope;
}

function resolveMaxAttempts(
  args: Record<string, unknown>,
  options: CallToolOptions,
): number {
  const hasIdempotency =
    typeof args.idempotency_key === "string" && args.idempotency_key.trim().length > 0;
  const retryable = options.retryable ?? hasIdempotency;
  if (!retryable) {
    return 1;
  }
  const requested = options.maxAttempts ?? 2;
  return Math.min(3, Math.max(1, Math.floor(requested)));
}

function isRetryableTransportFailure(err: unknown): boolean {
  if (!(err instanceof HubTransportError)) {
    return false;
  }
  if (err.status === undefined) {
    return true; // network / fetch failure
  }
  return TRANSIENT_HTTP.has(err.status);
}

function isAbortError(cause: unknown): boolean {
  return (
    typeof cause === "object" &&
    cause !== null &&
    "name" in cause &&
    (cause as { name?: string }).name === "AbortError"
  );
}
