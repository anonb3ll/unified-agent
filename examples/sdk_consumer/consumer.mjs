#!/usr/bin/env node
/**
 * sdk_consumer — external-facing governed consumer proof for the Unified
 * Agent typed SDK (`@unified-agent/client`).
 *
 * A standalone Node script, deliberately outside the agent fleet: it holds
 * only a bearer token and speaks to the hub over MCP streamable-HTTP through
 * the typed SDK client. It performs ONE approval-gated action —
 * `horizon2_package_register`, which the hub always gates behind a human
 * approval (`check_approval_gate(..., force=True)`) — then exports the
 * evidence bundle for that action to a JSON file.
 *
 * The approval path is real, not simulated:
 *   consumer call -> hub parks a `pending` approval row (call rejected)
 *   -> a human resolves it via approval_resolve (any authenticated operator
 *      surface: /ui, a fleet MCP session, etc.)
 *   -> consumer retries the identical call -> one-shot pass consumed
 *   -> governed_servers row written + `horizon2.connector.registered`
 *      recorded on the tamper-evident event hash chain.
 *
 * Everything the script writes into the evidence bundle is read back from
 * hub APIs (profile_status, approval_pending, horizon2_server_list,
 * event_log_query, event_log_verify, evidence_report) — no local fabrication.
 *
 * Required env:
 *   UA_HUB_URL             e.g. http://100.64.0.10:8765 (origin, no /mcp suffix needed)
 *   UA_AGENT_BEARER_TOKEN  per-agent bearer for a NON-Operator identity
 *   UA_AGENT_BEARER_TOKEN_FILE  optional path to read the token from instead
 *
 * Flags:
 *   --package NAME         connector package to register (default: granola)
 *   --server-id ID         governed_servers key; default sdk-consumer-<pkg>-<epoch>
 *   --review-ref REF       required review reference (default: task:1137-sdk-consumer-proof)
 *   --out PATH             evidence output file (default: ./evidence-<server_id>.json)
 *   --wait-seconds N       approval wait budget (default: 900)
 *   --poll-seconds N       approval poll interval (default: 10)
 *   --scope LIST           comma scopes subset of the package's (default: package default)
 *   --no-task              skip the bookkeeping task_add/task_update
 *
 * Exit codes: 0 success · 2 usage/env · 3 action denied · 4 approval timeout
 *             5 transport/auth/identity failure · 6 unexpected hub outcome
 */

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  HubAuthError,
  HubRpcError,
  HubTransportError,
  HubValidationError,
  UnifiedAgentClient,
} from "@unified-agent/client";

const SELF_DIR = fileURLToPath(new URL(".", import.meta.url));
const CONSUMER_VERSION = "0.1.0";
const GOVERNED_TOOL = "horizon2_package_register";
const OPERATOR_IDENTITIES = new Set(["operator", "desktop_person", "operator"]);

function log(...args) {
  console.log(`[${new Date().toISOString()}]`, ...args);
}

function usage(msg) {
  if (msg) console.error(`error: ${msg}`);
  console.error(
    "usage: node consumer.mjs [--package NAME] [--server-id ID] [--review-ref REF] " +
      "[--out PATH] [--wait-seconds N] [--poll-seconds N] [--scope a,b] [--no-task]",
  );
  process.exit(2);
}

function parseArgs(argv) {
  const opts = { scope: undefined, noTask: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const take = () => {
      const value = argv[++i];
      if (value === undefined) usage(`flag ${arg} needs a value`);
      return value;
    };
    if (arg === "--package") opts.package = take();
    else if (arg === "--server-id") opts.serverId = take();
    else if (arg === "--review-ref") opts.reviewRef = take();
    else if (arg === "--out") opts.out = take();
    else if (arg === "--wait-seconds") opts.waitSeconds = Number(take());
    else if (arg === "--poll-seconds") opts.pollSeconds = Number(take());
    else if (arg === "--scope") opts.scope = take().split(",").map((s) => s.trim()).filter(Boolean);
    else if (arg === "--no-task") opts.noTask = true;
    else if (arg === "--help" || arg === "-h") usage();
    else usage(`unknown flag ${arg}`);
  }
  return opts;
}

function resolveToken() {
  const fromEnv = process.env.UA_AGENT_BEARER_TOKEN?.trim();
  if (fromEnv) return fromEnv;
  const file = process.env.UA_AGENT_BEARER_TOKEN_FILE?.trim();
  if (file) {
    try {
      const tok = readFileSync(file, "utf8").trim();
      if (tok) return tok;
    } catch (err) {
      usage(`cannot read UA_AGENT_BEARER_TOKEN_FILE=${file}: ${err.message}`);
    }
  }
  usage("UA_AGENT_BEARER_TOKEN (or UA_AGENT_BEARER_TOKEN_FILE) is required");
}

function hubMcpUrl() {
  const raw = process.env.UA_HUB_URL?.trim();
  if (!raw) usage("UA_HUB_URL is required (e.g. http://100.64.0.10:8765)");
  const origin = raw.replace(/\/+$/, "").replace(/\/mcp$/, "");
  return `${origin}/mcp`;
}

/** sha256:<hex> provenance over the consumer sources themselves. */
function selfProvenance() {
  const hash = createHash("sha256");
  for (const name of ["consumer.mjs", "package.json"]) {
    try {
      hash.update(readFileSync(resolve(SELF_DIR, name)));
    } catch {
      // Package metadata may not exist in every layout; provenance still
      // binds to whatever files are present.
    }
  }
  return `sha256:${hash.digest("hex")}`;
}

/** Pull `request N is queued` out of the hub's approval-gate error text. */
function queuedApprovalId(err) {
  const message = err instanceof Error ? err.message : String(err);
  const match = /request (\d+) is queued/.exec(message);
  return match ? Number(match[1]) : null;
}

function isAlreadyRegistered(err) {
  const message = err instanceof Error ? err.message : String(err);
  return /already registered/.test(message);
}

function sleep(seconds) {
  return new Promise((r) => setTimeout(r, seconds * 1000));
}

const ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const RUN_WINDOW_SLACK_MS = 5_000;

/**
 * Floor ULID for `ms`: 48-bit timestamp prefix + zeroed randomness. ULIDs
 * sort lexicographically by time, so `ulid > floor` means "events after this
 * instant". event_log_query is oldest-first with an exclusive-since cursor
 * and exposes no tail read, so a synthesized floor is the only run-scoped
 * `since` a remote consumer can construct.
 */
function ulidFloor(ms) {
  const chars = new Array(10);
  for (let i = 9, t = BigInt(ms); i >= 0; i--, t /= 32n) {
    chars[i] = ULID_ALPHABET[Number(t % 32n)];
  }
  return chars.join("") + "0".repeat(16);
}

/**
 * Hub storage timestamps are `isoformat(timespec="seconds")` — e.g.
 * `2026-09-19T08:38:55+00:00`. Passing a JS `Z`-with-millis ISO string into a
 * `>= since` comparison mis-sorts (`+` < `.`), so emit the storage shape.
 */
function storageIso(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/**
 * Wait until approval `id` leaves the `pending` bucket.
 * Returns {state: 'approved'|'gone', row} — 'gone' means denied/revoked/
 * consumed elsewhere; the caller re-issues the call to surface the outcome.
 */
async function waitForApproval(client, id, { waitSeconds, pollSeconds }) {
  const deadline = Date.now() + waitSeconds * 1000;
  let lastPending = null;
  while (Date.now() < deadline) {
    // No tool filter: the gate parks the row under whichever inner tool
    // called check_approval_gate (e.g. horizon2_package_register delegates
    // to horizon2_server_register), so filtering by our outer name can miss
    // it. Match on the approval id instead — rows are few.
    const queue = await client.approvalPending();
    const pending = queue.pending.find((row) => row.id === id);
    if (pending) {
      lastPending = pending;
      await sleep(pollSeconds);
      continue;
    }
    const approved = queue.approved_unconsumed.find((row) => row.id === id);
    if (approved) return { state: "approved", row: approved, lastPending };
    return { state: "gone", row: null, lastPending };
  }
  return { state: "timeout", row: null, lastPending };
}

async function exportEvidence(client, ctx) {
  const [servers, events, connectorEvents, verify, report] = await Promise.all([
    client.callTool("horizon2_server_list", { include_revoked: true }),
    client
      .eventLogQuery({
        channels: ["horizon2.connector.*", "agent.approval.*", "agent.task.*"],
        since: ctx.sinceUlid,
        limit: 100,
      })
      .catch((err) => ({ error: String(err.message ?? err) })),
    // The governed channel is sparse enough that an unscoped oldest-first
    // read still reaches it — this pins OUR registration event even when a
    // resumed run's window no longer covers the original write.
    client
      .eventLogQuery({ channels: ["horizon2.connector.*"], limit: 100 })
      .catch((err) => ({ error: String(err.message ?? err) })),
    client.eventLogVerify().catch((err) => ({ error: String(err.message ?? err) })),
    client
      .evidenceReport({ agent: ctx.identity, since: ctx.since, limit: 10 })
      .catch((err) => ({ error: String(err.message ?? err) })),
  ]);
  const serverRow = (servers.servers ?? []).find((s) => s.server_id === ctx.serverId) ?? null;
  const governedEvent =
    (connectorEvents.events ?? []).find((e) => e.payload?.server_id === ctx.serverId) ?? null;
  return {
    generated_at: new Date().toISOString(),
    consumer: {
      name: "sdk_consumer",
      version: CONSUMER_VERSION,
      sdk: "@unified-agent/client",
      note: "standalone external consumer; not part of the agent fleet",
    },
    identity: ctx.identity,
    governed_action: {
      tool: GOVERNED_TOOL,
      package: ctx.package,
      server_id: ctx.serverId,
      approval_id: ctx.approvalId ?? null,
      outcome: ctx.outcome,
      result: ctx.result ?? null,
    },
    approval_snapshots: ctx.approvalSnapshots,
    governed_server: serverRow,
    governed_event: governedEvent,
    tracking_task: ctx.task ?? null,
    event_log: {
      verification: verify,
      since_floor_ulid: ctx.sinceUlid,
      events,
    },
    evidence_report: report,
  };
}

export async function runConsumer({
  client,
  package: pkg = "granola",
  serverId,
  reviewRef = "task:1137-sdk-consumer-proof",
  out,
  waitSeconds = 900,
  pollSeconds = 10,
  scope,
  noTask = false,
  logFn = log,
}) {
  const startMs = Date.now();
  const ctx = {
    identity: null,
    package: pkg,
    serverId,
    reviewRef,
    startedAt: new Date(startMs).toISOString(),
    since: storageIso(startMs - RUN_WINDOW_SLACK_MS),
    sinceUlid: ulidFloor(startMs - RUN_WINDOW_SLACK_MS),
    outcome: "failed",
    approvalSnapshots: {},
    task: null,
  };

  // 1. Handshake + verified identity. The hub — not the client — decides who
  //    we are; profile_status reports what the bearer actually resolved to.
  await client.ping();
  const profile = await client.profileStatus();
  ctx.identity = profile.identity;
  logFn(`connected: identity=${profile.identity} profile=${profile.profile}`);
  if (OPERATOR_IDENTITIES.has(String(profile.identity).toLowerCase())) {
    throw new Error(
      `refusing to run the external consumer as ${JSON.stringify(profile.identity)}: ` +
        "the proof requires a non-Operator agent identity",
    );
  }

  ctx.serverId =
    serverId ?? `sdk-consumer-${pkg}-${Math.floor(Date.now() / 1000)}`.slice(0, 64);
  const outPath = out ?? `evidence-${ctx.serverId}.json`;
  const args = {
    package: pkg,
    provenance: selfProvenance(),
    review_ref: reviewRef,
    server_id: ctx.serverId,
    ...(scope ? { scopes: scope } : {}),
  };

  // 2. Bookkeeping task — anchors the run in the platform's task store so the
  //    evidence_report agent filter has a record. Not part of the governed
  //    action; the `claimed-by:` note is the documented attribution marker.
  if (!noTask) {
    const added = await client.taskAdd({
      title: `[sdk-consumer-demo] governed connector registration ${ctx.serverId}`,
      notes:
        `External SDK consumer proof run. governed_action=${GOVERNED_TOOL} ` +
        `server_id=${ctx.serverId} review_ref=${reviewRef}\n\nclaimed-by: ${profile.identity}`,
      priority: "low",
      lane: "default",
    });
    ctx.task = { id: added.id };
    logFn(`tracking task: id=${added.id}`);
  }

  // 3. The governed call. A fresh (tool, payload) pair is parked as a pending
  //    approval and the call is rejected — the proposal half of the path.
  logFn(`proposing governed action: ${GOVERNED_TOOL} ${JSON.stringify({ server_id: ctx.serverId, package: pkg })}`);
  try {
    ctx.result = await client.callTool(GOVERNED_TOOL, args);
    ctx.outcome = "executed_without_gate";
    logFn("action executed immediately (a live one-shot pass already existed)");
  } catch (err) {
    if (isAlreadyRegistered(err)) {
      ctx.outcome = "already_registered";
      logFn(`server ${ctx.serverId} is already registered — exporting existing evidence`);
    } else {
      const approvalId = queuedApprovalId(err);
      if (approvalId === null) throw err;
      ctx.approvalId = approvalId;
      logFn(
        `gated: approval request ${approvalId} is pending — ` +
          "a human must run approval_resolve before this call can execute",
      );

      const decision = await waitForApproval(client, approvalId, { waitSeconds, pollSeconds });
      ctx.approvalSnapshots.after_wait = decision.row ?? decision.lastPending ?? null;
      if (decision.state === "timeout") {
        ctx.outcome = "approval_timeout";
        const report = await exportEvidence(client, ctx);
        writeFileSync(outPath, JSON.stringify(report, null, 2) + "\n");
        logFn(`approval ${approvalId} still pending after ${waitSeconds}s; evidence at ${outPath}`);
        logFn("re-run with the same --server-id to resume once a human approves");
        return { exitCode: 4, outPath, ctx };
      }
      if (decision.state === "approved") {
        logFn(`approval ${approvalId} granted — retrying identical call to consume the pass`);
      } else {
        logFn(`approval ${approvalId} left the queue without a live pass — surfacing outcome`);
      }
      try {
        ctx.result = await client.callTool(GOVERNED_TOOL, args);
        ctx.outcome = "approved_executed";
        logFn(`governed action executed: approval_id=${ctx.result.approval_id} event_id=${ctx.result.event_id}`);
      } catch (retryErr) {
        if (/DENIED/.test(String(retryErr?.message ?? retryErr))) {
          ctx.outcome = "denied";
          ctx.result = { error: String(retryErr.message ?? retryErr) };
          logFn(`governed action DENIED by human: ${retryErr.message}`);
        } else {
          throw retryErr;
        }
      }
    }
  }

  // 4. Evidence export — every field is a hub API read, never local state.
  const report = await exportEvidence(client, ctx);
  writeFileSync(outPath, JSON.stringify(report, null, 2) + "\n");
  logFn(`evidence report written: ${outPath}`);

  if (ctx.task && ctx.outcome !== "approval_timeout") {
    await client
      .taskUpdate({
        id: ctx.task.id,
        status: "done",
        append_note:
          `sdk_consumer outcome=${ctx.outcome} approval_id=${ctx.approvalId ?? "n/a"} ` +
          `server_id=${ctx.serverId} evidence=${outPath}`,
      })
      .catch((err) => logFn(`tracking task close failed (non-fatal): ${err.message}`));
  }

  const exitCode = ctx.outcome === "denied" ? 3 : ctx.outcome === "failed" ? 6 : 0;
  return { exitCode, outPath, ctx };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const client = new UnifiedAgentClient({
    url: hubMcpUrl(),
    bearerToken: resolveToken(),
    clientName: "sdk-consumer",
    clientVersion: CONSUMER_VERSION,
  });
  try {
    const { exitCode } = await runConsumer({
      client,
      package: opts.package,
      serverId: opts.serverId,
      reviewRef: opts.reviewRef,
      out: opts.out,
      waitSeconds: opts.waitSeconds,
      pollSeconds: opts.pollSeconds,
      scope: opts.scope,
      noTask: opts.noTask,
    });
    process.exit(exitCode);
  } catch (err) {
    if (err instanceof HubAuthError) {
      console.error(`hub auth failed (HTTP ${err.status}) — check UA_AGENT_BEARER_TOKEN`);
      process.exit(5);
    }
    if (err instanceof HubValidationError) {
      console.error(`hub contract break: ${err.message}`);
      process.exit(5);
    }
    if (err instanceof HubTransportError || err instanceof HubRpcError) {
      console.error(`hub error: ${err.message}`);
      process.exit(5);
    }
    throw err;
  } finally {
    await client.close().catch(() => {});
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
