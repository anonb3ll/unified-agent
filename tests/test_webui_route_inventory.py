"""Characterization lock for Command Center route inventory (Conductor task 1.1).

Keeps `web_routes()` aligned with the audit in
`conductor/tracks/web_ui_command_center_20260821/evidence/01-route-audit.md`.
When you add or remove a route, update that evidence note and this freeze list.
Last amended 2026-09-10: dual-mount `/ui` (Vite) + `/ui/legacy` (vanilla) for
Wayfinder task #889; `/ui/{filename}` widened to `/ui/{path:path}`.
Last amended 2026-09-18 (task #1071): read-only platform slice — `GET
/api/platform/manifest` + `GET /api/platform/objects/{kind}[/{object_id}]`.
Last amended 2026-09-18 (task #1066): `GET /api/capability-catalog` for the
evidence-backed capability catalog consumer surface.
Last amended 2026-09-18 (task #1078): `GET /api/dispatch-routes` for the
read-only named-agent route preflight inventory.
Last amended 2026-09-18 (task #1117): Plans pane — `GET /api/plans`,
`GET /api/plans/{plan_id}`, `POST /api/action/plan` (CSRF-gated review).
Last amended 2026-09-18 (task #1123): Vault pane — `GET /api/vault/search`,
`GET /api/vault/page`, `GET /api/vault/ingest-status`, `GET /api/vault/briefing`
(read-only; no vault mutation routes).
Last amended 2026-09-18 (task #1147): Connectors pane — `GET /api/connectors`
(read-only Horizon-2 lifecycle; no register/revoke/authorize routes).
Last amended 2026-09-18 (task #1148): Proposals pane — `GET /api/proposals`,
`POST /api/action/proposal` (CSRF-gated file_task | dismiss).
Last amended 2026-09-19 (task #1136): Quota pane — `GET /api/quota`
(read-only model_quota_status projection; no credential_pool/dispatch mutations).
Last amended 2026-09-21 (task #1121): Credential Pool pane — `GET
/api/credential_pool` (read-only credential_pool_list metadata projection;
no register/revoke routes, and the payload is re-projected to a named
metadata field set so a secret value can never be forwarded).
Last amended 2026-09-21 (tasks #1183 and #1185): delivery and Slack status
panes — `GET /api/notify_delivery` and `GET /api/slack_gateway`.
Last amended 2026-09-21 (task #1182): Delivery Receipts pane — `GET
/api/dispatch_receipts` (read-only dispatch-run receipt projection).
Last amended 2026-09-22 (task #1282): authenticated SDK v0.4 task/evidence writes
and pending approval requests; approval resolution remains human-only.
Last amended 2026-09-21 (task #993): single-mount cutover — `/ui/legacy`,
`/ui/legacy/excalidraw/{path:path}`, `/ui/legacy/{filename}` and the
`/ui/excalidraw/{path:path}` compat alias removed; `/ui` + `/ui/{path:path}`
remain and a missing `packages/web/dist` is a hard 503.
Last amended 2026-09-22 (task #1259): Delivery SLO — `GET /api/delivery_slo`
(read-only Epic C #1188 exit-test metrics: dispatch success rate, p90 cycle
time, unattributed completions, CI green rate; no mutation route).
Last amended 2026-09-23 (task #1287): read-only provenance graph query —
`GET /api/platform/provenance-graph`.
Last amended 2026-09-25 (task #1350): authenticated routine trigger —
`POST /api/routines/{routine_id}/fire`.
Last amended 2026-09-28 (task #1348): read-only cost rollup and budget status —
`GET /api/costs`.
Last amended 2026-09-28 (task #1351): agent lifecycle —
`GET`/`POST /api/agent-lifecycle` (pause, resume, terminate; CSRF-guarded).
Last amended 2026-10-12 (tasks #1700/#2007): fenced operator claim release —
`POST /api/tasks/{task_id}/claim-release` (CSRF-guarded, task-write admission).
Last amended 2026-09-28 (task #1354): read-only per-agent scorecards —
`GET /api/scorecards`.
Last amended 2026-10-03 (task #1861): bearer-only hook inbox — `POST
/api/inbox/session` and `GET /api/inbox/summary`.
Last amended 2026-10-03 (task #1864): Messages pane — `GET
/api/messages/conversations`, `GET /api/messages/conversations/{conversation_id}`,
`GET /api/messages/sessions` and CSRF-gated `POST /api/messages`.
Last amended 2026-10-10 (task #1426): conversation handoff — CSRF-gated
`POST /api/messages/conversations/{conversation_id}/handoff` and read-only
`GET /api/messages/conversations/{conversation_id}/handoffs`.
Last amended 2026-10-10 (task #1738): team snapshot and restore —
`GET`/`POST /api/teams/snapshots`, read-only
`GET /api/teams/snapshots/{snapshot_id}/preview` and CSRF-gated
`POST /api/teams/snapshots/{snapshot_id}/restore`.
Last amended 2026-09-28 (task #1352): config revisions —
`GET /api/config-revisions` and CSRF-gated `POST /api/action/config-revision`.
Last amended 2026-09-28 (tasks #1349 and #1423): goals and task comments —
`GET`/`POST /api/goals`, `GET /api/goals/{goal_id}`,
`GET /api/tasks/{task_id}/why-chain`, `GET`/`POST /api/tasks/{task_id}/comments`.
Last amended 2026-09-23 (task #1263): Approvals drawer — `GET
/api/approvals/{id}` (read-only single-row detail: redacted payload, gate,
device, task lineage, pr_url/commit_sha when present; no mutation route —
resolution stays on the existing `POST /api/action/approval`).
Last amended 2026-09-23 (task #1305): governed work-product writes —
`POST /api/work-products` and `POST /api/work-products/{id}/release`.
Last amended 2026-09-24 (task #1322): approver passkeys — `GET|POST
/api/oauth/passkeys`, `POST /api/oauth/passkeys/options`, `POST
/api/oauth/passkeys/delete` (human identity on the approver's tailnet node, CSRF).
Last amended 2026-09-24 (task #1327): ua-node device plane — `POST
/api/node/register`, `/api/node/heartbeat`, `/api/node/claim` (long-poll),
`/api/node/receipt` (idempotent), and approval-gated `/api/node/dispatch`.
Last amended 2026-09-25 (task #1328): ACP adapter acceptors — `POST
/api/node/events` and `/api/node/permission`.
Last amended 2026-09-25 (task #1326): opt-in device-OAuth LLM broker —
`POST /api/llm/v1/chat/completions`.
Last amended 2026-09-25 (task #1324): brokered connector OAuth — authenticated
status/begin/disconnect routes and one-use public provider callback.
Last amended 2026-09-25 (task #1329): Sessions launcher — `GET /api/nodes` and
CSRF-gated `POST /api/action/node-session` (start|cancel).
Last amended 2026-09-25 (task #1330): hub-served harness config — `GET|POST
/api/node/config`, `GET /api/harness-config`, CSRF-gated
`POST /api/action/harness-config`.
Last amended 2026-09-29 (task #1419): ranked task search query box —
`GET /api/tasks/search` (read-only pass-through to `task_list(query=...)`;
no mutation route, substring `notes_contain` stays MCP-only).
Last amended 2026-09-29 (task #1460): Work Graph pane — read-only `GET
/api/work-graph` (goal -> plan -> task -> run/session/evidence projection,
`goal_id`/`cursor`/`page_size` query params; same gate as `task_list`).
Last amended 2026-09-30 (task #1640): read-only `GET /api/ledger/rows`
(`after_seq`/`limit`; raw event_log rows in ROWID order with `payload_raw` for
external hash-chain verifiers; same gate as `/api/events/stream`).
Last amended 2026-10-01 (task #1736): team specs — read-only `GET /api/teams`
and `GET /api/teams/plan`, and CSRF-gated, approval-gated
`POST /api/action/team-up`.
Also amended 2026-10-01 (task #1757): Operating pane — read-only `GET
/api/operating`, `GET /api/operating/resolve`, `GET /api/operating/sprints`,
and CSRF-gated `POST /api/action/operating-sprint` and
`POST /api/action/operating-run` (goal_manage gates; agent writes park as
`operating_settings` approvals). Profiles and office bindings stay on the
policy revision path; there is no HTTP commit route.
Last amended 2026-10-03 (task #1740): ACP follow-ups — CSRF-gated
`POST /api/action/node-followup` (queue|steer, idempotency key) and the device
plane `POST /api/node/followups` (a node pulls its claimed item's follow-ups).
Last amended 2026-10-03 (task #1741): resume and adopt — read-only
`GET /api/nodes/native-sessions` and CSRF-gated `POST /api/action/node-resume`
and `POST /api/action/node-adopt` (approval-gated node dispatches).
Last amended 2026-10-06 (task #1744): session-scoped hub MCP credentials —
the device plane `POST /api/node/session_credential` (device-code OAuth only,
off unless `UA_ACP_SESSION_MCP=on`) and the operator-only, CSRF-gated
`POST /api/action/session-credential-revoke`.
Last amended 2026-10-09 (task #1747): task subtree holds — `GET /api/tasks/holds`
(same gate as `task_list`) and CSRF-gated `POST /api/tasks/holds` and
`POST /api/tasks/holds/{hold_id}/release` (task_update and the task_hold
action, or the verified operator; release limited to the hold's creator or an
operator).
Last amended 2026-10-12 (task #1854): Approval Ledger re-request — CSRF-gated
`POST /api/approvals/ledger-rerequest` (the coordination_manage
approval_ledger_rerequest action, or the verified operator; queues a missed
row, never resolves it).
Last amended 2026-11-09 (task #2407): explicit review reads — CSRF-gated
`POST /api/review/reads` (humans and UA_REVIEW_READERS; the row's reader is
the verified identity).
Last amended 2026-10-15 (task #1867): Approval Ledger poll-now — CSRF-gated
`POST /api/approvals/ledger-poll` (the coordination_manage approval_ledger_poll
action, or the verified operator; runs one scheduled poll, never resolves).
Last amended 2026-10-14 (task #1873): idle-session wakes — the device plane
`POST /api/node/wake` (long poll, ids and counts only) and
`POST /api/node/wake/receipt` (delivered|failed|skipped).
Last amended 2026-10-14 (task #2068): `POST /api/node/wake/check`
(one-shot attempt-budget admission, owned-node authorization and CSRF).
Also amended 2026-10-14 (task #2050): the operator-only, CSRF-gated
`POST /api/sessions/{id}/wake-grant` (grant or revoke one session's wakes).
Last amended 2026-10-19 (task #1652): Runs pane — read-only `GET /api/runs`
and `GET /api/runs/{run_id}` (one uniform row per run across dispatch_runs,
the agent.run.selfpickup channel and agent_sessions; the run object reader's
ceiling, operator-exempt; no mutation routes).
Last amended 2026-10-20 (task #2144): per-tool call allowance — read-only
`GET /api/tool-allowance` (live rate-limit counters and today's ledger count;
per-caller fence, operator-only cross-identity read).
Last amended 2026-10-15 (task #2112): device acceptance — read-only
`GET /api/nodes/acceptance` and CSRF-gated `POST /api/action/node-acceptance`
(start|evaluate; the coordination_manage node_acceptance actions, or the
verified operator; the node's own agent or a lifecycle operator only).
Last amended 2026-10-22 (task #2208): node retire — CSRF-gated
`POST /api/action/node-retire` (retire|unretire; the coordination_manage
node_retire/node_unretire actions, or the verified operator; lifecycle
operators only).
Last amended 2026-11-10 (task #2393): work proposals — CSRF-gated
`POST /api/action/work-proposal-decide` (the coordination_manage
work_proposal_decide action, or the verified operator; work_proposals admits a
human identity only).
Last amended 2026-11-10 (task #2469): Control Center v2 adapters — read-only
`GET /api/memory/note`, `GET /api/vault/list`, `GET /api/vault/links`,
`GET /api/plans/{plan_id}/joint`, and CSRF-gated memory, vault, plan (one route
per plan_manage action), task claim/delete and session end
writes (`webui/cc_knowledge.py`, `webui/cc_work.py`).
Last amended 2026-11-10 (task #2480): Control Center v2 hub operations —
CSRF-gated plan structure (step-add, step-edit, step-remove, reorder, edit),
goal (edit, link, unlink, close, reopen) and operator session-end writes
(`webui/cc_work.py`); read-only `GET /api/routines`, routine add, pause, resume
and Run now, and the LangGraph workflow run (`webui/cc_runs.py`).

Last amended 2026-11-11 (task #2501): keyed launches — read-only
`GET /api/launch-attempts/{key}` and the operator-only, CSRF-gated
`POST /api/dispatch-runs/{run_id}/settle-spawn` (`webui/cc_launch.py`).
Last amended 2026-11-11 (task #2470): Control Center v2 agents — operator-only
`GET /api/agent-roster` and CSRF-gated agent add/remove, Horizon-2 connector
register, register-package, authorize and revoke, and the org-proposal
review/apply/reject route (`webui/cc_agents.py`).
Last amended 2026-11-11 (task #2514): Control Center v2 Home and Inbox — read-only
`GET /api/work-proposals` and `GET /api/work-proposals/{proposal_id}`, and the
operator-only, CSRF-gated `POST /api/sessions/{id}/wake-decline` and
`POST /api/action/review-hold` (`webui/cc_inbox.py`).
Last amended 2026-11-12 (task #2523): Control Center v2 Agents hub gaps — read-only
`GET /api/agent-seats` (the roster without credential columns, any signed-in
reader) and `GET /api/agents/{label}/profile` (profile ceiling and assigned
skills on the skill_registry ceiling) (`webui/cc_agents.py`).

"""

from __future__ import annotations

from unified_agent.webui.routes import web_routes

# Frozen 2026-08-21 against live hub system_doctor (web_ui LIVE, 36 routes).
EXPECTED_ROUTES: list[tuple[str, frozenset[str]]] = [
    ("/healthz", frozenset({"GET"})),
    ("/readyz", frozenset({"GET"})),
    ("/ui", frozenset({"GET"})),
    ("/ui/{path:path}", frozenset({"GET"})),
    ("/api/session", frozenset({"POST", "DELETE"})),
    ("/api/session/options", frozenset({"GET"})),
    ("/api/session/workos/login", frozenset({"GET"})),
    ("/api/session/workos/callback", frozenset({"GET"})),
    ("/api/routines/{routine_id}/fire", frozenset({"POST"})),
    ("/api/slack/events", frozenset({"POST"})),
    ("/api/whoami", frozenset({"GET"})),
    ("/api/overview", frozenset({"GET"})),
    ("/api/attention", frozenset({"GET"})),
    ("/api/digest", frozenset({"GET"})),
    ("/api/agents", frozenset({"GET"})),
    ("/api/personas", frozenset({"GET"})),
    ("/api/personas/{label}", frozenset({"GET"})),
    ("/api/org", frozenset({"GET"})),
    ("/api/dispatch-routes", frozenset({"GET"})),
    ("/api/agents/{label}", frozenset({"GET"})),
    ("/api/sessions", frozenset({"GET"})),
    ("/api/sessions/{id}", frozenset({"GET"})),
    ("/api/runs", frozenset({"GET"})),
    ("/api/runs/{run_id}", frozenset({"GET"})),
    ("/api/sessions/{id}/crew", frozenset({"GET"})),
    ("/api/sessions/{id}/wake-grant", frozenset({"POST"})),
    ("/api/tasks", frozenset({"GET", "POST"})),
    ("/api/tasks/next-work-projection", frozenset({"POST"})),
    ("/api/tasks/search", frozenset({"GET"})),
    ("/api/tasks/holds", frozenset({"GET", "POST"})),
    ("/api/tasks/holds/{hold_id}/release", frozenset({"POST"})),
    ("/api/tasks/{task_id}/claim-release", frozenset({"POST"})),
    ("/api/work-products", frozenset({"POST"})),
    ("/api/work-products/{id}/release", frozenset({"POST"})),
    ("/api/board", frozenset({"GET"})),
    ("/api/goals", frozenset({"GET", "POST"})),
    ("/api/goals/{goal_id}", frozenset({"GET"})),
    ("/api/tasks/{task_id}/why-chain", frozenset({"GET"})),
    ("/api/tasks/{task_id}/comments", frozenset({"GET", "POST"})),
    ("/api/work-graph", frozenset({"GET"})),
    ("/api/flow-metrics", frozenset({"GET"})),
    ("/api/queue-health", frozenset({"GET"})),
    ("/api/projects", frozenset({"GET"})),
    ("/api/review-approval", frozenset({"GET"})),
    ("/api/executor-delegation", frozenset({"GET"})),
    ("/api/approvals", frozenset({"GET"})),
    ("/api/approvals/request", frozenset({"POST"})),
    ("/api/approvals/ledger-rerequest", frozenset({"POST"})),
    ("/api/approvals/ledger-poll", frozenset({"POST"})),
    ("/api/review/reads", frozenset({"POST"})),
    ("/api/approvals/{id}", frozenset({"GET"})),
    ("/api/memory", frozenset({"GET"})),
    ("/api/vault/search", frozenset({"GET"})),
    ("/api/vault/page", frozenset({"GET"})),
    ("/api/vault/ingest-status", frozenset({"GET"})),
    ("/api/vault/briefing", frozenset({"GET"})),
    ("/api/connectors", frozenset({"GET"})),
    ("/api/connectors/oauth/{provider}", frozenset({"GET"})),
    ("/api/connectors/oauth/{provider}/begin", frozenset({"POST"})),
    ("/api/connectors/oauth/{provider}/disconnect", frozenset({"POST"})),
    ("/oauth/connectors/callback", frozenset({"GET"})),
    ("/api/quota", frozenset({"GET"})),
    ("/api/credential_pool", frozenset({"GET"})),
    ("/api/slack_gateway", frozenset({"GET"})),
    ("/api/notify_delivery", frozenset({"GET"})),
    ("/api/dispatch_receipts", frozenset({"GET"})),
    ("/api/agent-graph", frozenset({"GET"})),
    ("/api/zulip", frozenset({"GET"})),
    ("/api/execution-graph", frozenset({"GET"})),
    ("/api/dispatch-stack", frozenset({"GET"})),
    ("/api/cycles", frozenset({"GET"})),
    ("/api/cycles/{cycle_id}", frozenset({"GET"})),
    ("/api/plans", frozenset({"GET"})),
    ("/api/plans/{plan_id}", frozenset({"GET"})),
    ("/api/proposals", frozenset({"GET"})),
    ("/api/org-proposals", frozenset({"GET"})),
    ("/api/org-proposals/{proposal_id}", frozenset({"GET"})),
    ("/api/health", frozenset({"GET"})),
    ("/api/cloud", frozenset({"GET"})),
    ("/api/devices", frozenset({"GET"})),
    ("/api/nodes", frozenset({"GET"})),
    ("/api/nodes/native-sessions", frozenset({"GET"})),
    ("/api/nodes/acceptance", frozenset({"GET"})),
    ("/api/mobile/v1/projection", frozenset({"GET"})),
    ("/api/langgraph/checkpoints", frozenset({"GET"})),
    ("/api/evidence", frozenset({"GET", "POST"})),
    ("/api/platform/manifest", frozenset({"GET"})),
    ("/api/platform/objects/{kind}", frozenset({"GET"})),
    ("/api/platform/objects/{kind}/{object_id}", frozenset({"GET"})),
    ("/api/platform/provenance-graph", frozenset({"GET"})),
    ("/api/capability-catalog", frozenset({"GET"})),
    ("/api/perf", frozenset({"GET"})),
    ("/api/delivery_slo", frozenset({"GET"})),
    ("/api/config-revisions", frozenset({"GET"})),
    ("/api/operating", frozenset({"GET"})),
    ("/api/operating/resolve", frozenset({"GET"})),
    ("/api/operating/sprints", frozenset({"GET"})),
    ("/api/skills-teams", frozenset({"GET"})),
    ("/api/teams", frozenset({"GET"})),
    ("/api/teams/plan", frozenset({"GET"})),
    ("/api/teams/snapshots", frozenset({"GET", "POST"})),
    ("/api/teams/snapshots/{snapshot_id}/preview", frozenset({"GET"})),
    ("/api/teams/snapshots/{snapshot_id}/restore", frozenset({"POST"})),
    ("/api/costs", frozenset({"GET"})),
    ("/api/tool-allowance", frozenset({"GET"})),
    ("/api/scorecards", frozenset({"GET"})),
    ("/api/events", frozenset({"GET"})),
    ("/api/telemetry", frozenset({"GET"})),
    ("/api/events/stream", frozenset({"GET"})),
    ("/api/ledger/rows", frozenset({"GET"})),
    ("/api/action/approval", frozenset({"POST"})),
    ("/api/action/config-revision", frozenset({"POST"})),
    ("/api/action/operating-sprint", frozenset({"POST"})),
    ("/api/action/operating-run", frozenset({"POST"})),
    ("/api/action/skills-teams", frozenset({"POST"})),
    ("/api/action/team-up", frozenset({"POST"})),
    ("/api/action/task", frozenset({"POST"})),
    ("/api/agent-lifecycle", frozenset({"GET", "POST"})),
    ("/api/action/workflow", frozenset({"POST"})),
    ("/api/action/dispatch", frozenset({"POST"})),
    ("/api/action/dispatch-stack", frozenset({"POST"})),
    ("/api/action/dependency", frozenset({"POST"})),
    ("/api/action/handoff", frozenset({"POST"})),
    ("/api/action/crew-intervene", frozenset({"POST"})),
    ("/api/action/node-session", frozenset({"POST"})),
    ("/api/action/node-followup", frozenset({"POST"})),
    ("/api/action/node-resume", frozenset({"POST"})),
    ("/api/action/node-adopt", frozenset({"POST"})),
    ("/api/action/node-acceptance", frozenset({"POST"})),
    ("/api/action/node-retire", frozenset({"POST"})),
    ("/api/action/work-proposal-decide", frozenset({"POST"})),
    ("/api/action/cycle", frozenset({"POST"})),
    ("/api/action/plan", frozenset({"POST"})),
    ("/api/action/proposal", frozenset({"POST"})),
    ("/api/action/fleet", frozenset({"POST"})),
    ("/api/action/oauth-revoke", frozenset({"POST"})),
    ("/api/action/session-credential-revoke", frozenset({"POST"})),
    ("/api/action/harness-config", frozenset({"POST"})),
    ("/api/oauth/passkeys", frozenset({"GET", "POST"})),
    ("/api/oauth/passkeys/options", frozenset({"POST"})),
    ("/api/oauth/passkeys/delete", frozenset({"POST"})),
    ("/api/mobile/v1/action", frozenset({"POST"})),
    ("/api/graph-canvas", frozenset({"GET", "PUT"})),
    ("/api/session-capture", frozenset({"POST"})),
    ("/api/inbox/session", frozenset({"POST"})),
    ("/api/inbox/session/owed", frozenset({"POST"})),
    ("/api/inbox/summary", frozenset({"GET"})),
    ("/api/messages", frozenset({"POST"})),
    ("/api/messages/conversations", frozenset({"GET"})),
    ("/api/messages/conversations/{conversation_id}", frozenset({"GET"})),
    ("/api/messages/conversations/{conversation_id}/handoff", frozenset({"POST"})),
    ("/api/messages/conversations/{conversation_id}/handoffs", frozenset({"GET"})),
    ("/api/messages/sessions", frozenset({"GET"})),
    ("/api/node/register", frozenset({"POST"})),
    ("/api/node/heartbeat", frozenset({"POST"})),
    ("/api/node/claim", frozenset({"POST"})),
    ("/api/node/receipt", frozenset({"POST"})),
    ("/api/node/wake", frozenset({"POST"})),
    ("/api/node/wake/check", frozenset({"POST"})),
    ("/api/node/wake/receipt", frozenset({"POST"})),
    ("/api/node/dispatch", frozenset({"POST"})),
    ("/api/node/events", frozenset({"POST"})),
    ("/api/node/permission", frozenset({"POST"})),
    ("/api/node/session_credential", frozenset({"POST"})),
    ("/api/node/followups", frozenset({"POST"})),
    ("/api/node/config", frozenset({"GET", "POST"})),
    ("/api/harness-config", frozenset({"GET"})),
    ("/api/llm/v1/chat/completions", frozenset({"POST"})),
    ("/api/memory/note", frozenset({"GET"})),
    ("/api/action/memory/save", frozenset({"POST"})),
    ("/api/action/memory/delete", frozenset({"POST"})),
    ("/api/action/memory/restore", frozenset({"POST"})),
    ("/api/action/memory/promote", frozenset({"POST"})),
    ("/api/action/memory/feedback", frozenset({"POST"})),
    ("/api/vault/list", frozenset({"GET"})),
    ("/api/vault/links", frozenset({"GET"})),
    ("/api/action/vault/write", frozenset({"POST"})),
    ("/api/action/vault/journal", frozenset({"POST"})),
    ("/api/action/plan/create", frozenset({"POST"})),
    ("/api/action/plan/update", frozenset({"POST"})),
    ("/api/action/plan/checkpoint", frozenset({"POST"})),
    ("/api/action/plan/resume", frozenset({"POST"})),
    ("/api/action/plan/retry", frozenset({"POST"})),
    ("/api/action/plan/claim", frozenset({"POST"})),
    ("/api/action/plan/handoff", frozenset({"POST"})),
    ("/api/action/plan/close", frozenset({"POST"})),
    ("/api/action/plan/delete", frozenset({"POST"})),
    ("/api/action/plan/joint/bind", frozenset({"POST"})),
    ("/api/action/plan/joint/propose", frozenset({"POST"})),
    ("/api/action/plan/joint/review", frozenset({"POST"})),
    ("/api/action/plan/joint/decide", frozenset({"POST"})),
    ("/api/action/plan/joint/recover", frozenset({"POST"})),
    ("/api/plans/{plan_id}/joint", frozenset({"GET"})),
    ("/api/action/task/claim", frozenset({"POST"})),
    ("/api/action/task/delete", frozenset({"POST"})),
    ("/api/action/session/end", frozenset({"POST"})),
    ("/api/action/plan/step-add", frozenset({"POST"})),
    ("/api/action/plan/step-edit", frozenset({"POST"})),
    ("/api/action/plan/step-remove", frozenset({"POST"})),
    ("/api/action/plan/reorder", frozenset({"POST"})),
    ("/api/action/plan/edit", frozenset({"POST"})),
    ("/api/action/goal/edit", frozenset({"POST"})),
    ("/api/action/goal/link", frozenset({"POST"})),
    ("/api/action/goal/unlink", frozenset({"POST"})),
    ("/api/action/goal/close", frozenset({"POST"})),
    ("/api/action/goal/reopen", frozenset({"POST"})),
    ("/api/action/session/operator-end", frozenset({"POST"})),
    ("/api/action/routine/add", frozenset({"POST"})),
    ("/api/action/routine/pause", frozenset({"POST"})),
    ("/api/action/routine/resume", frozenset({"POST"})),
    ("/api/action/routine/run", frozenset({"POST"})),
    ("/api/action/langgraph/run", frozenset({"POST"})),
    ("/api/routines", frozenset({"GET"})),
    ("/api/work-proposals", frozenset({"GET"})),
    ("/api/work-proposals/{proposal_id}", frozenset({"GET"})),
    ("/api/sessions/{id}/wake-decline", frozenset({"POST"})),
    ("/api/action/review-hold", frozenset({"POST"})),
    ("/api/agent-seats", frozenset({"GET"})),
    ("/api/agents/{label}/profile", frozenset({"GET"})),
    ("/api/launch-attempts/{key}", frozenset({"GET"})),
    ("/api/dispatch-runs/{run_id}/settle-spawn", frozenset({"POST"})),
    ("/api/agent-roster", frozenset({"GET"})),
    ("/api/action/agent/add", frozenset({"POST"})),
    ("/api/action/agent/remove", frozenset({"POST"})),
    ("/api/action/connector/authorize", frozenset({"POST"})),
    ("/api/action/connector/register", frozenset({"POST"})),
    ("/api/action/connector/register-package", frozenset({"POST"})),
    ("/api/action/connector/revoke", frozenset({"POST"})),
    ("/api/action/org-proposal", frozenset({"POST"})),
    # Control Center v2 hub gaps (task #2516).
    ("/api/tasks/{task_id}/subtree", frozenset({"GET"})),
    ("/api/langgraph/runs/{run_id}", frozenset({"GET"})),
    ("/api/session-credentials", frozenset({"GET"})),
    ("/api/evidence/export", frozenset({"GET"})),
]


def _live_route_map() -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for route in web_routes():
        methods = frozenset(m for m in (route.methods or set()) if m not in {"HEAD"})
        # session is registered twice (POST + DELETE) — merge methods per path
        out[route.path] = out.get(route.path, frozenset()) | methods
    return out


def test_web_routes_match_inventory_freeze() -> None:
    live = _live_route_map()
    expected = {path: methods for path, methods in EXPECTED_ROUTES}
    assert live == expected, (
        f"route inventory drift.\n"
        f"only_live={sorted(set(live) - set(expected))}\n"
        f"only_expected={sorted(set(expected) - set(live))}\n"
        f"method_mismatches="
        f"{ {p: (live[p], expected[p]) for p in set(live) & set(expected) if live[p] != expected[p]} }"
    )

    assert sum(1 for _ in web_routes()) == 238
