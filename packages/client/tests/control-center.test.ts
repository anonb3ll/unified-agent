import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { z } from "zod";

import { ControlCenterClient } from "../src/control-center.ts";
import type { FetchLike } from "../src/hub.ts";
import { HubAuthError, HubTransportError, HubValidationError } from "../src/errors.ts";

function jsonResponse(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
  };
}

describe("ControlCenterClient", () => {
  const manifest = {
    schema_version: "1",
    product: "unified-agent",
    object_types: [
      {
        kind: "task",
        uri: "unified-agent://tasks/{id}",
        name: "task",
        title: "Task",
        description: "A task",
        mime_type: "application/json",
        collection: false,
        served: true,
        table: "tasks",
        id_param: "id",
        id_pattern: "[0-9]+",
        profile_tool: "task_list",
        lifecycle: ["open", "done"],
        terminal_states: ["done"],
        event_kinds: ["agent.task.updated"],
      },
    ],
  };

  const catalog = {
    available: true,
    manifest,
    catalog: {
      schema_version: "1",
      families: [{ id: "governance" }],
      items: [
        {
          id: "audit",
          consumer: "operator",
          connection_state: "unknown",
          entry_point: { kind: "mcp-tool", ref: "profile_status", note: "live check" },
          evidence: [{ type: "code", ref: "src/unified_agent/tools/profiles.py" }],
        },
      ],
      queue_coverage: [],
      traceable_paths: [],
    },
    meta: {
      as_of: "2026-09-18T00:00:00Z",
      source_baseline_sha: "abc",
      origin_main_sha: "def",
      live_hub_checked_at: "2026-09-18T00:00:00Z",
      total_items: 1,
      pilot_families: [],
      counts_by_state: { unknown: 1 },
      counts_by_implementation: { shipped: 1 },
      source: "catalog.json",
    },
    status: 200,
  };

  it("discovers the manifest and static capability evidence through read-only GETs", async () => {
    const calls: string[] = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url) => {
        calls.push(url);
        return jsonResponse(200, url.endsWith("manifest") ? manifest : catalog);
      },
    });
    assert.equal((await client.platformManifest()).object_types[0]?.kind, "task");
    const capabilityCatalog = await client.capabilityCatalog();
    assert.equal(capabilityCatalog.catalog?.items[0]?.id, "audit");
    assert.deepEqual(capabilityCatalog.manifest, manifest);
    assert.deepEqual(calls, ["http://hub/api/platform/manifest", "http://hub/api/capability-catalog"]);
  });

  it("rejects malformed discovery payloads", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(200, { available: true }),
    });
    await assert.rejects(() => client.capabilityCatalog());
  });

  it("distinguishes forbidden and unavailable discovery", async () => {
    const forbidden = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(403, { error: "forbidden" }),
    });
    await assert.rejects(() => forbidden.platformManifest(), HubAuthError);
    const unavailable = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(503, { error: "offline" }),
    });
    await assert.rejects(() => unavailable.capabilityCatalog(), /HTTP 503/);
  });

  it("fetches and validates the read-only queue-health surface", async () => {
    const calls: Array<{ url: string; headers?: Record<string, string> }> = [];
    const fetchMock: FetchLike = async (url, init) => {
      calls.push({ url, headers: init?.headers });
      return jsonResponse(200, {
        desktop_open: [
          {
            id: 1,
            title: "open",
            status: "open",
            platform: { kind: "task", id: "1", uri: "unified-agent://tasks/1" },
            links: {
              agent: { kind: "agent", id: "Desktop_Agent2", uri: "unified-agent://agents/Desktop_Agent2" },
              run: null,
              approvals: [],
            },
          },
        ],
        doing: [],
        waiting_review: [],
        stale_claims: [],
        laptop_lane: [],
        blocked_archived: [],
        inconsistent: [],
        other: [{ id: 2, title: "future", status: "new_status" }],
        counts: {
          desktop_open: 1,
          doing: 0,
          waiting_review: 0,
          stale_claims: 0,
          laptop_lane: 0,
          blocked_archived: 0,
          inconsistent: 0,
          other: 1,
          total_active: 1,
        },
      });
    };

    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: fetchMock,
    });
    const health = await client.queueHealth();

    assert.deepEqual(health.counts, {
      desktop_open: 1,
      dispatch_ready: 0,
      interactive_only: 0,
      operator_only: 0,
      doing: 0,
      waiting_review: 0,
      stale_claims: 0,
      laptop_lane: 0,
      blocked_archived: 0,
      inconsistent: 0,
      other: 1,
      total_active: 1,
    });
    assert.equal(health.other[0]?.status, "new_status");
    assert.equal(health.desktop_open[0]?.platform?.uri, "unified-agent://tasks/1");
    assert.equal(health.desktop_open[0]?.links?.agent?.id, "Desktop_Agent2");
    assert.deepEqual(calls, [
      {
        url: "http://hub/api/queue-health",
        headers: {
          Accept: "application/json",
          Authorization: "Bearer tok",
        },
      },
    ]);
  });

  for (const [name, buckets, counts, expectedId, expectedCount] of [
    ["legacy", { legacy_operator_only: [{ id: 71, title: "Operator decision", status: "open", lane: "legacy-operator-only" }] }, { legacy_operator_only: 1 }, 71, 1],
    ["canonical", { operator_only: [{ id: 72, title: "Operator decision", status: "open", lane: "operator-only" }] }, { operator_only: 1 }, 72, 1],
    ["dual", {
      operator_only: [{ id: 72, title: "Operator decision", status: "open", lane: "operator-only" }],
      legacy_operator_only: [{ id: 71, title: "Legacy decision", status: "open", lane: "legacy-operator-only" }],
    }, { operator_only: 1, legacy_operator_only: 9 }, 72, 1],
    ["empty canonical", { operator_only: [], legacy_operator_only: [{ id: 71, title: "Legacy decision", status: "open" }] }, { operator_only: 0, legacy_operator_only: 1 }, undefined, 0],
  ] as const) {
    it(`normalizes ${name} operator buckets without duplicating legacy rows or counts`, async () => {
      const client = new ControlCenterClient({
        baseUrl: "http://hub", bearerToken: "tok",
        fetch: async () => jsonResponse(200, {
          desktop_open: [], doing: [], waiting_review: [], stale_claims: [],
          laptop_lane: [], blocked_archived: [], inconsistent: [], other: [],
          ...buckets,
          counts: {
            desktop_open: 0, doing: 0, waiting_review: 0, stale_claims: 0,
            laptop_lane: 0, blocked_archived: 0, inconsistent: 0, other: 0,
            total_active: expectedCount, ...counts,
          },
        }),
      });
      const health = await client.queueHealth();
      assert.equal(health.operator_only[0]?.id, expectedId);
      assert.equal(health.operator_only.length, expectedCount);
      assert.equal(health.counts.operator_only, expectedCount);
      assert.equal(health.counts.total_active, expectedCount);
      assert.equal("legacy_operator_only" in health, false);
      assert.equal("legacy_operator_only" in health.counts, false);
    });
  }

  for (const [buckets, counts] of [
    [{ operator_only: null, legacy_operator_only: [] }, { operator_only: 0 }],
    [{ operator_only: [], legacy_operator_only: [] }, { operator_only: -1, legacy_operator_only: 0 }],
  ]) {
    it("rejects invalid canonical operator data rather than hiding it behind a legacy alias", async () => {
      const client = new ControlCenterClient({
        baseUrl: "http://hub", bearerToken: "tok",
        fetch: async () => jsonResponse(200, {
          desktop_open: [], doing: [], waiting_review: [], stale_claims: [],
          laptop_lane: [], blocked_archived: [], inconsistent: [], other: [],
          ...buckets,
          counts: {
            desktop_open: 0, doing: 0, waiting_review: 0, stale_claims: 0,
            laptop_lane: 0, blocked_archived: 0, inconsistent: 0, other: 0,
            total_active: 0, ...counts,
          },
        }),
      });
      await assert.rejects(() => client.queueHealth(), z.ZodError);
    });
  }

  it("follows a queue task ref to its authenticated platform object", async () => {
    const calls: Array<{ url: string; headers?: Record<string, string> }> = [];
    const taskEnvelope = z.object({
      uri: z.literal("unified-agent://tasks/41"),
      task: z.object({ id: z.number(), title: z.string() }).passthrough(),
    });
    const fetchMock: FetchLike = async (url, init) => {
      calls.push({ url, headers: init?.headers });
      if (url.endsWith("/api/queue-health")) {
        return jsonResponse(200, {
          desktop_open: [
            { id: 41, title: "open", status: "open", platform: { kind: "task", id: "41", uri: "https://attacker.example/object/41" } },
          ],
          doing: [], waiting_review: [], stale_claims: [], laptop_lane: [], blocked_archived: [], inconsistent: [], other: [],
          counts: { desktop_open: 1, doing: 0, waiting_review: 0, stale_claims: 0, laptop_lane: 0, blocked_archived: 0, inconsistent: 0, other: 0, total_active: 1 },
        });
      }
      return jsonResponse(200, { uri: "unified-agent://tasks/41", task: { id: 41, title: "open" } });
    };
    const client = new ControlCenterClient({ baseUrl: "http://hub", bearerToken: "tok", fetch: fetchMock });
    const health = await client.queueHealth();
    const ref = health.desktop_open[0]?.platform;
    assert.ok(ref);
    const detail = await client.platformObject(ref, taskEnvelope);

    assert.equal(detail.task.id, 41);
    assert.deepEqual(calls, [
      { url: "http://hub/api/queue-health", headers: { Accept: "application/json", Authorization: "Bearer tok" } },
      { url: "http://hub/api/platform/objects/task/41", headers: { Accept: "application/json", Authorization: "Bearer tok" } },
    ]);
  });

  it("encodes platform object IDs and rejects unsafe segments", async () => {
    const calls: string[] = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url) => {
        calls.push(url);
        return jsonResponse(200, { ok: true });
      },
    });
    const schema = z.object({ ok: z.boolean() });
    await client.platformObject({ kind: "evidence_event", id: "a/b", uri: "https://attacker.example/" }, schema);
    assert.equal(calls[0], "http://hub/api/platform/objects/evidence_event/a%2Fb");
    for (const bad of ["", " ", ".", "..", "bad\nvalue"]) {
      await assert.rejects(
        () => client.platformObject({ kind: bad, id: "41", uri: "https://attacker.example/" }, schema),
        HubValidationError,
      );
    }
  });

  it("fails closed for platform object HTTP and schema errors", async () => {
    const ref = { kind: "task", id: "41", uri: "https://attacker.example/" };
    for (const [status, error] of [[401, HubAuthError], [403, HubAuthError], [404, HubTransportError]] as const) {
      const client = new ControlCenterClient({ baseUrl: "http://hub", bearerToken: "tok", fetch: async () => jsonResponse(status, {}) });
      await assert.rejects(() => client.platformObject(ref, z.unknown()), error);
    }
    const malformed = new ControlCenterClient({ baseUrl: "http://hub", bearerToken: "tok", fetch: async () => ({ ok: true, status: 200, headers: { get: () => "application/json" }, text: async () => "not-json" }) });
    await assert.rejects(() => malformed.platformObject(ref, z.unknown()), HubTransportError);
    const invalid = new ControlCenterClient({ baseUrl: "http://hub", bearerToken: "tok", fetch: async () => jsonResponse(200, { wrong: true }) });
    await assert.rejects(() => invalid.platformObject(ref, z.object({ ok: z.boolean() })), HubValidationError);
    const runtimeInvalid = new ControlCenterClient({ baseUrl: "http://hub", bearerToken: "tok", fetch: async () => jsonResponse(200, {}) });
    await assert.rejects(() => runtimeInvalid.platformObject({ kind: 1, id: "41", uri: "https://attacker.example/" } as never, z.unknown()), HubValidationError);
  });

  it("fails closed when the read-only endpoint rejects the bearer token", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "bad",
      fetch: async () => jsonResponse(401, { error: "authentication required" }),
    });

    await assert.rejects(() => client.queueHealth(), HubAuthError);
  });

  it("rejects a malformed queue-health payload", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(200, { counts: {} }),
    });

    await assert.rejects(() => client.queueHealth());
  });

  it("writes tasks through authenticated routes without caller-supplied identity", async () => {
    const calls: Array<{ url: string; method?: string; headers?: Record<string, string>; body?: string }> = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url, init) => {
        calls.push({ url, method: init?.method, headers: init?.headers, body: init?.body });
        return jsonResponse(201, url.endsWith("/api/tasks") ? { id: 51 } : { id: 51, status: "doing" });
      },
    });

    assert.deepEqual(await client.createTask({ title: "SDK task", project: "unified-agent" }), { id: 51 });
    assert.deepEqual(await client.updateTask({ id: 51, status: "doing" }), { id: 51, status: "doing" });
    assert.deepEqual(calls.map(({ url, method }) => [url, method]), [
      ["http://hub/api/tasks", "POST"],
      ["http://hub/api/action/task", "POST"],
    ]);
    assert.equal(JSON.parse(calls[0].body ?? "{}").priority, "med");
    for (const call of calls) {
      assert.equal(call.headers?.Authorization, "Bearer tok");
      assert.equal(call.headers?.["Content-Type"], "application/json");
      assert.equal("actor" in JSON.parse(call.body ?? "{}"), false);
      assert.equal("identity" in JSON.parse(call.body ?? "{}"), false);
    }
  });

  it("rejects whitespace-only task titles before sending a write", async () => {
    const calls: string[] = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url) => {
        calls.push(url);
        return jsonResponse(200, { id: 52 });
      },
    });

    await assert.rejects(
      () => client.updateTask({ id: 52, title: " \t " }),
      HubValidationError,
    );
    await assert.rejects(() => client.createTask({ title: " \t " }), HubValidationError);
    assert.deepEqual(calls, []);
  });

  it("submits and releases work products through the hub with approval-bound inputs", async () => {
    const calls: Array<{ url: string; body?: string; authorization?: string }> = [];
    const pending = { status: "pending", approval_id: 18, work_product_id: 27 };
    const released = {
      id: 27,
      title: "Synthetic memo",
      content_sha256: "a".repeat(64),
      source_path: "/workspace/drafts/memo.md",
      size_bytes: 18,
      mime_type: "text/markdown",
      producing_agent: "Desktop_Agent5",
      producing_model: "synthetic-model",
      template_pack: "legal",
      tags: { matter: "M-synthetic", client: "Example Client" },
      status: "released",
      previous_version_id: null,
      approval_id: 18,
      released_path: "/workspace/released/27-memo.md",
      released_sha256: "a".repeat(64),
      reject_reason: null,
      submitted_by: "Desktop_Agent5",
      resolved_by: "Desktop_Person",
      resolved_at: "2026-09-23T11:00:00Z",
      released_at: "2026-09-23T11:01:00Z",
      created_at: "2026-09-23T10:59:00Z",
      updated_at: "2026-09-23T11:01:00Z",
    };
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url, init) => {
        calls.push({ url, body: init?.body, authorization: init?.headers?.Authorization });
        return url.endsWith("/release") ? jsonResponse(200, released) : jsonResponse(202, pending);
      },
    });
    assert.deepEqual(await client.submitWorkProduct({
      source_path: "/workspace/drafts/memo.md",
      title: "Synthetic memo",
      template_pack: "legal",
      tags: { matter: "M-synthetic", client: "Example Client" },
      model: "synthetic-model",
    }), pending);
    assert.deepEqual(await client.releaseWorkProduct(27), released);
    assert.deepEqual(calls.map(({ url }) => url), [
      "http://hub/api/work-products",
      "http://hub/api/work-products/27/release",
    ]);
    assert.deepEqual(JSON.parse(calls[1]?.body ?? "null"), {});
    assert.equal(calls[0]?.authorization, "Bearer tok");
    assert.equal(calls[1]?.authorization, "Bearer tok");
  });

  it("rejects a malformed released document hash from the hub", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(200, {
        id: 27,
        title: "Synthetic memo",
        content_sha256: "a".repeat(64),
        source_path: "/workspace/drafts/memo.md",
        size_bytes: 18,
        mime_type: "text/markdown",
        producing_agent: "Desktop_Agent5",
        producing_model: null,
        template_pack: "legal",
        tags: {},
        status: "released",
        previous_version_id: null,
        approval_id: 18,
        released_path: "/workspace/released/27-memo.md",
        released_sha256: "malformed",
        reject_reason: null,
        submitted_by: "Desktop_Agent5",
        resolved_by: "Operator",
        resolved_at: "2026-09-23T11:00:00Z",
        released_at: "2026-09-23T11:01:00Z",
        created_at: "2026-09-23T10:59:00Z",
        updated_at: "2026-09-23T11:01:00Z",
      }),
    });
    await assert.rejects(() => client.releaseWorkProduct(27), HubValidationError);
  });

  it("rejects forged work-product identity and release inputs before sending", async () => {
    const calls: string[] = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url) => { calls.push(url); return jsonResponse(202, { status: "pending", approval_id: 18, work_product_id: 27 }); },
    });
    await assert.rejects(() => client.submitWorkProduct({
      source_path: "/workspace/x", title: "Memo", template_pack: "legal", producing_agent: "Operator",
    } as never), HubValidationError);
    await assert.rejects(() => client.submitWorkProduct({
      source_path: "/workspace/x", title: " \t ", template_pack: "legal",
    }), HubValidationError);
    await assert.rejects(() => client.releaseWorkProduct(0), HubValidationError);
    assert.deepEqual(calls, []);
  });

  it("queues approval requests without resolving or executing them", async () => {
    let request: Record<string, unknown> | undefined;
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (_url, init) => {
        request = JSON.parse(init?.body ?? "{}") as Record<string, unknown>;
        return jsonResponse(202, { approval_id: 7, status: "pending", tool: "task_update", payload: { id: 51 } });
      },
    });

    const pending = await client.requestApproval({ tool: "task_update", payload: { id: 51 } });
    assert.deepEqual(pending, { approval_id: 7, status: "pending", tool: "task_update", payload: { id: 51 } });
    assert.equal(request?.decision, undefined);
    assert.equal(request?.resolved_by, undefined);
  });

  it("returns a typed pending approval when a task or evidence write is gated", async () => {
    const pending = { approval_id: 7, status: "pending", tool: "task_add", payload: { title: "SDK task" } };
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(202, pending),
    });
    assert.deepEqual(await client.createTask({ title: "SDK task" }), pending);
    assert.deepEqual(await client.createEvidence({ channel: "sdk.task", payload: {} }), pending);
  });

  it("resolves approvals with an explicit human decision and appends attributable evidence", async () => {
    const calls: Array<{ url: string; body?: string }> = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url, init) => {
        calls.push({ url, body: init?.body });
        if (url.endsWith("/api/action/approval")) return jsonResponse(200, { id: 7, status: "approved" });
        return jsonResponse(201, { event_ulid: "01JTEST", inserted: true });
      },
    });

    assert.deepEqual(await client.resolveApproval({ id: 7, decision: "approve", reason: "reviewed" }), { id: 7, status: "approved" });
    assert.deepEqual(await client.createEvidence({ channel: "sdk.task", payload: { task_id: 51 } }), { event_ulid: "01JTEST", inserted: true });
    assert.deepEqual(calls.map(({ url }) => url), ["http://hub/api/action/approval", "http://hub/api/evidence"]);
    for (const call of calls) {
      const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
      assert.equal(body.originator, undefined);
      assert.equal(body.actor, undefined);
      assert.equal(body.resolved_by, undefined);
    }
  });

  it("rejects caller identity fields and surfaces forbidden write routes", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(403, { error: "forbidden" }),
    });

    await assert.rejects(
      () => client.createTask({ title: "bad", actor: "Operator" } as never),
      HubValidationError,
    );
    await assert.rejects(
      () => client.createEvidence({ channel: "sdk.task", payload: {}, originator: "Operator" } as never),
      HubValidationError,
    );
    await assert.rejects(() => client.createEvidence({ channel: "task", payload: {} }), HubValidationError);
    await assert.rejects(() => client.resolveApproval({ id: 7, decision: "approve" }), HubAuthError);
  });

  it("lists nodes and starts/cancels node sessions (task #1329)", async () => {
    const calls: Array<{ url: string; method?: string; body?: string }> = [];
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url, init) => {
        calls.push({ url, method: init?.method, body: init?.body as string | undefined });
        if (String(url).endsWith("/api/nodes")) {
          return jsonResponse(200, {
            nodes: [
              {
                node: "desk-01",
                agent: "Desktop_Agent1",
                backends: ["codex"],
                harnesses: {},
                worktree_roots: ["/tmp/repo"],
              },
            ],
            recent_items: [],
          });
        }
        if (String(url).endsWith("/api/action/node-session")) {
          const body = JSON.parse(String(init?.body ?? "{}")) as { action?: string; item_id?: string };
          if (body.action === "cancel") {
            return jsonResponse(200, { item_id: body.item_id, cancel_requested: true });
          }
          return jsonResponse(201, { item_id: "nd-abc12345", node: "desk-01", status: "queued" });
        }
        if (String(url).includes("/api/sessions")) {
          return jsonResponse(200, { sessions: [], open: 0, total: 0 });
        }
        return jsonResponse(404, {});
      },
    });

    const nodes = await client.listNodes();
    assert.equal(nodes.nodes[0]?.node, "desk-01");
    const started = await client.startNodeSession({
      node: "desk-01",
      prompt: "hi",
      cwd: "/tmp/repo",
      backend: "codex",
    });
    assert.equal(started.item_id, "nd-abc12345");
    const cancelled = await client.cancelNodeSession("nd-abc12345");
    assert.equal(cancelled.cancel_requested, true);
    const sessions = await client.listSessions({ status: "open" });
    assert.deepEqual(sessions.sessions, []);
    assert.deepEqual(
      calls.map(({ url, method, body }) => ({
        url,
        method,
        body: body === undefined ? undefined : JSON.parse(body),
      })),
      [
        { url: "http://hub/api/nodes", method: "GET", body: undefined },
        {
          url: "http://hub/api/action/node-session",
          method: "POST",
          body: { action: "start", node: "desk-01", prompt: "hi", cwd: "/tmp/repo", backend: "codex" },
        },
        {
          url: "http://hub/api/action/node-session",
          method: "POST",
          body: { action: "cancel", item_id: "nd-abc12345" },
        },
        { url: "http://hub/api/sessions?status=open", method: "GET", body: undefined },
      ],
    );
  });

  it("lists runs with filters and a cursor, and reads one run", async () => {
    const calls: string[] = [];
    const listed = runRow({ failure_reason: "disk" });
    const fetched = runRow({
      run_id: "01SESSION",
      kind: "session",
      task_id: null,
      session_kind: "acp",
      links: {
        self: { kind: "session", id: "01SESSION", uri: "unified-agent://sessions/01SESSION" },
        task: null,
        dispatch_run: null,
        evidence: [],
      },
    });
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async (url) => {
        calls.push(url);
        const path = new URL(url).pathname;
        if (path === "/api/runs") {
          return jsonResponse(200, {
            runs: [listed],
            next_cursor: "2026-10-19T00:00:00+00:00|run-1-aa",
            count: 1,
            truncated: true,
            limit: 2,
          });
        }
        if (path === "/api/runs/01SESSION") {
          return jsonResponse(200, { run: fetched });
        }
        return jsonResponse(404, { error: "unknown run" });
      },
    });

    const page = await client.listRuns({
      kind: "dispatch",
      agent: "worker",
      task_id: 41,
      limit: 2,
      cursor: "2026-10-19T00:00:00+00:00|run-0-aa",
    });
    assert.equal(page.runs.length, 1);
    assert.equal(page.runs[0]?.run_id, "run-1-aa");
    assert.equal(page.runs[0]?.failure_reason, "disk");
    assert.equal(page.next_cursor, "2026-10-19T00:00:00+00:00|run-1-aa");
    assert.equal(page.truncated, true);
    const listedUrl = new URL(calls[0] ?? "");
    assert.equal(listedUrl.pathname, "/api/runs");
    assert.equal(listedUrl.searchParams.get("kind"), "dispatch");
    assert.equal(listedUrl.searchParams.get("agent"), "worker");
    assert.equal(listedUrl.searchParams.get("task_id"), "41");
    assert.equal(listedUrl.searchParams.get("limit"), "2");
    assert.equal(listedUrl.searchParams.get("cursor"), "2026-10-19T00:00:00+00:00|run-0-aa");

    const run = await client.getRun("01SESSION");
    assert.equal(run.kind, "session");
    assert.equal(run.task_id, null);
    assert.equal(run.session_kind, "acp");
    assert.equal(calls[1], "http://hub/api/runs/01SESSION");
  });

  it("maps an unknown run to the transport not-found error", async () => {
    const client = new ControlCenterClient({
      baseUrl: "http://hub",
      bearerToken: "tok",
      fetch: async () => jsonResponse(404, { error: "unknown run" }),
    });
    await assert.rejects(
      () => client.getRun("missing-run"),
      (err: unknown) => {
        assert.ok(err instanceof HubTransportError);
        assert.equal(err.status, 404);
        assert.equal(err.message, "control center HTTP 404");
        return true;
      },
    );
  });
});

function runRow(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-1-aa",
    kind: "dispatch",
    task_id: 1,
    agent: "worker",
    status: "running",
    outcome: null,
    started_at: "2026-10-19T00:00:00+00:00",
    ended_at: null,
    lease_expires_at: null,
    abandoned: false,
    abandon_reason: null,
    links: {
      self: { kind: "run", id: "run-1-aa", uri: "unified-agent://runs/run-1-aa" },
      task: { kind: "task", id: "1", uri: "unified-agent://tasks/1" },
      delegation: null,
      evidence: [],
    },
    ...overrides,
  };
}
