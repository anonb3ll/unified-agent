# Provenance graph

Task #1287. The evidence ledger already stores the 5-link accountability
chain — `human -> device -> agent -> action -> proof` — and
[evidence-report.md](evidence-report.md) renders it as a table/report. This
document covers the other shape over the same ledger: a **read model** of
nodes and edges, a **query API** over it, an appendix on the evidence
report, and the Control Center graph view.

No new storage. Nothing here is written independently — every graph is
derived from `tasks`, `approvals`, `delegations`, `event_log`, and
`dispatch_runs` (via delivery receipts) at read time, the same tables
`evidence_report.build_evidence_report` already reads. See
`src/unified_agent/audit/provenance_graph.py` for the implementation;
`evidence_report.py` imports it for the report appendix, and
`platform_http.py` imports it for the HTTP query surface — never the
other direction, so the two modules cannot form an import cycle.

## Read model

### Node kinds

| Kind | Id | Populated from | Status |
| --- | --- | --- | --- |
| `human` | `human:<identity>` | Evidence report's `human`/`human_provenance`, and each approval's `resolved_by` | live |
| `device` | `device:<node or host>` | `device_link` (verified tailnet node, or an unverified hostname trail) | live |
| `agent` | `agent:<label>` | Evidence report's `agent` (task `claimed_by` / event `originator`) | live |
| `task` | `task:<id>` | The task row (`title`, `status`, `priority`, `project`) | live |
| `action` | `action:task:<id>` | The task's executed action (`type`, `status`, `lane`) | live |
| `approval` | `approval:<id>` | One row of `approvals` (`tool`, `status`, `reason`) | live |
| `work_product` | `work_product:<pr_url\|commit_sha\|evidence_ref>` | Evidence report's `proof`, including the event-log hashes it certifies | live |
| `model` | `model:<name>` | No backing column anywhere in the ledger today | **declared, not populated — gap** |
| `release` | `release:<id>` | No release/git-tag record in the ledger today | **declared, not populated — gap** |

`model` and `release` are declared in the node vocabulary (matching the
task's node-kind list) but never emitted: nothing in `events.py`,
`storage.py`, or `producer_sign.py` records a release/git-tag, and no
delivery receipt or dispatch run carries a per-action model attribution
today. Rather than fabricate one, the graph simply omits those node kinds —
the same "declared but not served" honesty rule `object_model.py` already
uses for platform object types. `graph_for_release` (below) documents the
concrete workaround for the missing release record.

### Edges

| Edge | Meaning |
| --- | --- |
| `human -[used_device]-> device` | The record's resolved human used this device |
| `device -[ran_agent]-> agent` | The device ran this agent |
| `human -[ran_agent]-> agent` | Fallback when no device link exists at all (marked `device_verified: false` in edge attrs) |
| `agent -[performed]-> action` | The agent performed this action |
| `task -[has_action]-> action` | The task's executed action |
| `action -[produced]-> work_product` | The action produced this PR/commit/evidence artifact |
| `action -[gated_by]-> approval` | The action was gated by this approval |
| `human -[resolved]-> approval` | This human resolved the approval (may be a different human than the record's primary chain human) |

Node ids are `"{kind}:{value}"`, so the same human/device/agent/work-product
named across multiple records collapses into one node instead of
duplicating it. Edge identity is `(source, target, relation)`; attrs merge
(last write wins) when the same edge recurs.

### Determinism and the canonical hash

`canonical_hash(nodes, edges)` sorts nodes by `(kind, id)` and edges by
`(relation, source, target)`, serializes with `json.dumps(..., sort_keys=True,
separators=(",", ":"))`, and takes the SHA-256 hex digest. Two builds from
the same ledger state — or a build, a JSON round trip, and a rebuild — always
produce the same hash. A ledger event that is tampered with or removed
changes the hash even when the tampering doesn't touch a node/edge key
directly: `work_product` nodes carry the report's `tamper_evident` flag and
the matched `event_log` entry hashes as attrs, and both are sensitive to the
underlying event row. See `tests/test_provenance_graph.py` for the pinned
determinism and tamper-detection tests.

## Query API

Two named queries, plus the plain task/project/time-window scope the
evidence-report appendix uses.

### (a) Everything a human authorized in a time range

"Authorized" means either the record's resolved chain human is the queried
one, or they approved at least one approval on the task — a human can gate
work they did not personally execute. Only that human's own approvals
appear as nodes; someone else's approval on the same task is dropped
entirely (not merely unlinked), because this query answers "what did *this*
human authorize", not "what happened on tasks this human touched at all".
`limit` caps the final matching task records (newest task id first); the
response scope sets `truncated: true` when more matches exist.

```
GET /api/platform/provenance-graph?query=by_human&human=Desktop_Person&since=2026-09-01&until=2026-09-30
unified-agent platform provenance-graph --query by_human --human Desktop_Person --since 2026-09-01 --until 2026-09-30
```

### (b) Every action behind a release

**Documented gap**: the ledger has no release/git-tag record today. Until
one exists, this query is keyed on a task id or an explicit comma-separated
set of task ids — the release's constituent tasks, tracked by whoever cuts
the release — rather than a release identity the ledger could resolve on
its own. When a release record lands in the ledger, `graph_for_release`
should be rewritten to resolve its task set instead of accepting one from
the caller.

```
GET /api/platform/provenance-graph?query=by_release&task_ids=1180,1181,1182
unified-agent platform provenance-graph --query by_release --task-ids 1180,1181,1182
```

### Plain scope (task / project / time window)

```
GET /api/platform/provenance-graph?task_id=777
GET /api/platform/provenance-graph?project_id=819
unified-agent platform provenance-graph --task-id 777
```

### Response shape

```json
{
  "schema_version": "1.0",
  "scope": {"kind": "task_scope", "task_id": 777},
  "scope_hash": "...",
  "node_count": 7,
  "edge_count": 7,
  "node_counts_by_kind": {
    "action": 1, "agent": 1, "approval": 1, "device": 1, "human": 1,
    "task": 1, "work_product": 1
  },
  "nodes": [{"id": "task:777", "kind": "task", "label": "777", "attrs": {"...": "..."}}],
  "edges": [{"source": "agent:agent-cursor", "target": "action:task:777", "relation": "performed", "attrs": {}}],
  "canonical_hash": "..."
}
```

### Transports

- **HTTP**: `GET /api/platform/provenance-graph`, alongside the existing
  `/api/platform/objects/{kind}[/{id}]` read surface
  (`src/unified_agent/platform_http.py:provenance_graph_payload`,
  `src/unified_agent/webui/routes.py:platform_provenance_graph`). Same
  origin/auth guard as every other `/api` route, same profile gate as
  `event_log_query` (the same gate `evidence_event` reads use — the graph is
  derived from the same ledger tables), and the same `agent_access`
  operation-class check as `platform_object` (`platform_object:` prefix, so
  managed-secret mode allows it as a bounded platform read).
- **CLI**: `unified-agent platform provenance-graph [--query by_human|by_release]
  [--task-id N] [--task-ids N,N,...] [--project-id N] [--since ISO] [--until ISO]
  [--human LABEL] [--agent LABEL] [--pr URL] [--limit N]`
  (`src/unified_agent/platform_cli.py`), same `UA_HTTP_HUB_URL` +
  `HubClient` bearer-auth plumbing as `unified-agent platform get`.
- **No MCP tool.** Per the Epic B tool-budget rule (default profile capped,
  see `src/unified_agent/profiles.py`), this surface ships as an HTTP read
  endpoint and a CLI subcommand only — it does not grow the default MCP
  tool count. If a future MCP tool is added for it, it must be gated into a
  non-default profile (`profiles.py` + `tool_classification.py`).

## Evidence report appendix

`evidence_report.build_evidence_report()` gains a `provenance_graph` field
(added at report schema `1.3`, current schema `1.7` —
see [evidence-report.md](evidence-report.md#json-schema)):
exactly `provenance_graph.build_graph_from_records()` run over the report's
own `records`, scoped to the same task/project/time-window filter. Markdown
and text renderers gain a "Provenance graph" / "PROVENANCE GRAPH" section
with the node/edge counts, the canonical hash, and the edge list. CSV and
PDF exports are unchanged — the appendix is JSON/Markdown/text only, since a
graph edge list does not fit a CSV row or the PDF's per-record layout.

## Control Center view

The Control Center page `/ui/settings/audit/provenance`
(`packages/web/src/app/settings/ProvenancePage.tsx`, under Settings → Audit;
the classic `?view=provenance` link redirects there) is a reviewer-driven
query tool over `GET /api/platform/provenance-graph`. It supports all three
query shapes:

- **By human** — human + optional since/until/limit (`query=by_human`).
- **By release** — a task id or comma-separated task ids
  (`query=by_release`).
- **Plain scope** — task id, project id, since/until, agent, PR, limit (the
  default, no `query` param).

The graph is drawn on the shared `GraphCanvas` primitive, with nodes grouped
by kind in accountability-chain order (human → device → agent → task/action
→ approval/work product, `src/lib/provenance/types.ts:KIND_ORDER`) and a table
fallback that lists every node and edge for screen readers. The page also
shows node and edge counts, the per-kind counts (`node_counts_by_kind`), and
the `canonical_hash` ("Graph fingerprint").

A `human` or `device` node whose `attrs.verified` is not literally `true`
is marked **"not verified"** rather than looking like a
confirmed identity — anything other than `verified: true` (missing,
`false`, or absent) reads as unverified in the UI. This is the UI half of
#1217: the backend already refuses to promote a verified *agent* signer to
a `human` node (see `provenance_graph.py`'s `_is_human_actor_kind`), and the
page never overrides or hides that distinction by rendering an unverified
node as if it were confirmed.

A failed read (400 for bad query params, 403 when profile- or
feature-gated, 404 for an unresolvable release task set) renders an error
state with the reason and Retry; an empty match renders an explicit
"Nothing matches" message rather than a blank page. The Settings area tests (`packages/web/tests/app/settings/`,
`tests/app/settings.e2e.spec.ts`) cover the query form, rendering and
states.

Submitted filters are synchronized to the page URL so a reviewer can reload or
share the same query. `datetime-local` inputs are displayed in local time while
the filter state and the request keep absolute UTC instants, so a shared link
reproduces the exact window it names, seconds included. For `by_release`, the
two selectors are alternatives — editing one clears the other — and when a URL
carries both, the values are merged into `task_ids` instead of one being
dropped, because the API gives a non-empty `task_ids` list precedence over the
singular `task_id`. The page does not poll or subscribe to live updates;
apply the query again to rerun it.

## Testing

`tests/test_provenance_graph.py` covers: rebuild-from-ledger determinism
(same hash twice, and after a JSON round trip), a tampered ledger event
changing the hash, a removed ledger event changing the hash, the two named
queries' subgraphs on a synthetic fixture, and a synthetic fixture shaped
like the #747 chain (human approves -> agent action -> proof).
`tests/test_webui_platform.py::TestProvenanceGraphRoute` and
`tests/test_platform_cli.py` cover the HTTP route and CLI subcommand.
