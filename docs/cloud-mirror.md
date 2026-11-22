# Cloud mirror (task #1285, PoC)

A metadata-only, one-way mirror of a narrow, allowlisted slice of the local
hub's `event_log` (`unified_agent.events`) to a small, separately-deployed
read-only receiver. It exists so a remote timeline of operational metadata —
"what happened, to which task, by which agent, when" — can be inspected
without the hub itself being reachable, and without any notes, message
bodies, file contents, diffs, secrets, or paths ever leaving the box.

This is a proof of concept: lean, not a framework. Off by default at every
layer. **Nothing in this change is deployed or activated** — see "Not
activated" below.

## Pieces

- **Shipper** — `src/unified_agent/cloud_mirror.py`. Reads `event_log` after
  a persisted ULID cursor, keeps only allowlisted channels, projects each
  event into a metadata-only record via an explicit field allowlist, and
  hands batches to a `Sink`. `run_once()` is the entry point; example (not
  installed) systemd timer at `deploy/systemd/ua-cloud-mirror.{service,timer}`
  and macOS LaunchAgent at `deploy/mac-hub/com.unified-agent.cloud-mirror.plist`.
  A missing cursor starts the initial sync; an existing malformed or unreadable
  cursor fails closed before querying the event log, preventing accidental
  full-history replay to a custom sink. The receiver validates source event IDs
  as canonical ULIDs because pagination and latest-state queries depend on
  their lexicographic time ordering.
- **Receiver** — `src/unified_agent/cloud_mirror_receiver.py`. A minimal
  Starlette ASGI app (Starlette is already a transitive dependency via
  `mcp[cli]`; see `pilot_app.py`/`oauth.py`/`server.py` for the same hand-rolled
  pattern elsewhere in this repo) with SQLite storage, a write-only shipper
  credential for ingest and a separate operator credential for reads/export/
  delete, dedupe-by-`event_id` ingest, and read-only query routes. SQLite work
  runs in Starlette's worker pool so database latency does not block the ASGI
  event loop; the standalone entry point binds the container interface for
  Cloud Run. Schema and indexes are initialized once when the ASGI app is
  constructed, not on each request. Agent health breaks equal timestamp ties
  by ULID, so the reported channel is deterministic. Every route still requires
  its distinct bearer authorization.
- **Connector manifest** — `CLOUD_MIRROR_MANIFEST` in `connectors.py`
  (Epic G), `default_enabled=False`, no unconditionally-required secret (see
  the manifest's own comment for why).

## Event channels allowlisted, and where each one comes from

Found by reading `src/unified_agent/events.py` (channel constants + the
`_KIND_CHANNELS` map that `storage.record_agent_event`'s mirror uses) and
grepping every `record_event(` / `events.record_event(` call site in
`src/unified_agent/**/*.py`:

| Channel | Source | Covers |
|---|---|---|
| `agent.task.updated` | `events.CHANNEL_TASK_UPDATED`; emitted via `storage.record_agent_event(kind="task", ...)` from `tools/tasks.py` (`task_add`, `task_update`, `task_claim`, release paths) | task created / updated / claimed / released |
| `agent.approval.requested` | `events.CHANNEL_APPROVAL_REQUESTED`; emitted via `storage.record_agent_event(kind="approval", ...)` | approval requested |
| `agent.approval.elicitation` | `elicitation.ELICITATION_CHANNEL`, emitted from `elicitation.py`'s `_log()` on every elicitation state change | approval requested / approved / denied / timed out |
| `agent.cycle.updated` | literal channel string in `tools/cycles.py` (`autonomous_cycle_start`/`_transition`) | closest available structured proxy for "test/audit result" — autonomous verification-cycle state transitions |
| `agent.health.alert` | `events.CHANNEL_HEALTH_ALERT`; emitted via `storage.record_agent_event(kind="executor", ...)` | executor/health signal (closest available proxy for "agent heartbeat" — see limitation below) |
| `agent.project.built` | `events.CHANNEL_PROJECT_BUILT`, emitted from `tools/tasks.py`'s `project_build` | build completion |

Everything else is never read past the channel filter in `events.query_events`
— most importantly `agent.message.*` (`messaging/schema.py`'s
`CHANNEL_MESSAGE_PREFIX`, message bodies) and `agent.journal.appended`
(`storage.record_agent_event(kind="journal", ...)`, journal text).

## Approval notification hints

The hub approval-request producer adds `tool` and `requested_by` to the local
`agent.approval.requested` payload, retaining `kind`, `ref_id` and `summary`.
`tool` is a bounded ASCII identifier (including dotted and colon-delimited
internal action names),
or `null` for invalid text. `requested_by`, the event originator and request
activity attribution use the
verified request identity, or `null` when none is verified; caller-supplied
requester labels are not notification identity. Approval payloads, commands,
titles and reasons are never copied into these request hints. Invalid tool text
also yields the generic summary `queued`.

These additive fields are absent from legacy activity mirrors, including
approval resolution activity historically published on the same channel.
Consumers must handle missing or null fields. The cloud projection below drops
both new fields and retains its existing allowlist. Hub profile gates are
unchanged; this event shape grants no new read or approval authority. MCP event
reads and platform approval detail readers retain their profile gates. The direct
Web UI SSE route currently authenticates requests without applying that profile
ceiling; do not treat its raw event stream as a redacted restricted-profile view.

Desktop notifications should remain generic by default. A notification is a
hint to refetch the approval by `ref_id` through the authenticated, profile-gated
hub detail reader before displaying sensitive detail or making any decision.
Do not approve from event data, a historical cursor replay or a notification
button alone: the approval may have changed since the event. The existing
approval/evidence writers remain authoritative. This server contract does not
implement or certify native notifications, notification clicks, profile-switch
behavior or OS disclosure controls; those require separate native acceptance.

## Field allowlist

Every projected record has exactly these keys, never more
(`_ALLOWED_RECORD_KEYS` in both `cloud_mirror.py` and
`cloud_mirror_receiver.py`, the latter enforced again on ingest):

```
schema_version   int, currently 1
event_id         str, the source event's ULID -- also the dedupe key
channel          str, one of the allowlisted channels above
kind             str | null, one of "task" / "approval" / "cycle" / "health" / "build"
agent            str | null, the event's originator
task_id          str | null, the channel's own reference id (task id, approval id, or cycle id)
status           str | null, a structured, enum-validated status token -- see "Status" below for where each kind's comes from
timestamp        str, the event_log row's created_at
```

**No free text ever crosses this boundary.** The single biggest risk here is
that `storage.record_agent_event`'s activity-index mirror — which is what
actually produces `agent.task.updated`, `agent.approval.requested`, and
`agent.health.alert` — always carries a `summary` field that embeds real
task/approval titles verbatim, e.g. `tools/tasks.py`:
`summary=f"created ({priority}): {title}"` and
`summary=f"status {old['status']}->{status}: {row['title']}"`. **That field
is never read by any projector, for any channel, under any circumstance** —
see `_mirrored_kind_fields()` in `cloud_mirror.py`.

### Status: where each kind's comes from

`status` is never parsed out of `summary` prose, full stop. It is populated
three different ways depending on `kind`, all structured:

- **`kind == "task"` and `kind == "approval"`** — **status as of ship time**,
  not as of the mirrored event. `run_once`'s `_enrich_structured_statuses`
  runs, at most, two extra batched read-only queries per shipper run (never
  one per event) against local storage: `SELECT id, status FROM tasks WHERE
  id IN (...)` for every task-kind record's `task_id` in the batch, and the
  same shape against `approvals` for every approval-kind record's `task_id`.
  Each returned value is checked against that table's own known enum before
  being accepted — `tasks.status`'s CHECK constraint
  (`open`/`doing`/`waiting_review`/`blocked`/`done`/`cancelled`, read via
  `tools.tasks.STATUSES` so it can't drift from the real enum) and
  `approvals.status`'s CHECK constraint
  (`pending`/`approved`/`denied`/`consumed`/`revoked`/`expired`) — anything
  outside that set is dropped back to `null` rather than passed through.
  Storage being unreachable at ship time (locked DB, missing file) is caught
  and logged; every affected record simply keeps `status: null` — this
  lookup can never fail the run. Because this is a live snapshot rather than
  the historical value at event time, two `agent.task.updated` records for
  the same task shipped in the same batch will carry the *same* (current)
  status, even if one event represents an earlier transition than the other.
- **`agent.approval.elicitation`** — normalized to `kind == "approval"`
  so both approval event channels share one pending-state group. A recognized
  `elicitation.py` action is carried only when it belongs to the fixed action
  allowlist; structured approval status is backfilled from the approval row.
- **`kind == "cycle"`** — also read directly off the payload:
  `transition.to_state` / `cycle.state`, both state-machine enum tokens
  `tools/cycles.py` itself defines.
- **`kind == "health"`** — always `null`. There is no structured status
  source for executor/health activity at all (see "Known PoC limitations").

## Acceptance mapping

| Requirement | How it's met |
|---|---|
| Local hub usable when cloud is down | Sink and cursor writes are caught and reported by `run_once()`; cursor failures keep the prior cursor. FileSink dedupes retries by `event_id`. Nothing in the hub's request/tool path calls into `cloud_mirror` — it is an out-of-band batch job. |
| No secret, message body, note, file content, diff, or unlisted path ever leaves the box | Channel allowlist (`ALLOWLISTED_CHANNELS`) + strict receiver channel/kind/status validation + field allowlist (`_ALLOWED_RECORD_KEYS`) + per-channel projector functions that read named payload keys only, never the whole payload. `tests/test_cloud_mirror.py::test_projection_never_leaks_summary_text` plants a fake secret in a task title and asserts the projected record's JSON never contains it. |
| Every record has agent/task/timestamp/schema_version | Enforced structurally: `project_event()` always sets all four top-level keys (task_id may legitimately be `None` for a channel with no natural id), and the receiver's `_validate_record` rejects any ingest record missing `schema_version`/`event_id`/`channel`/`timestamp`. |
| Dedupe | `event_id` (the source ULID) is the receiver's SQLite primary key; ingest uses `INSERT OR IGNORE`, so a shipper retry after a lost response is a no-op on the receiver side, and a resend after a shipper crash mid-batch never double-counts. |
| Export + delete | `GET /export` (full JSON dump) and `DELETE /data` (wipe) require the operator-only `UA_CLOUD_MIRROR_RECEIVER_TOKEN`; the shipper's separate `UA_CLOUD_MIRROR_INGEST_TOKEN` is accepted only by `POST /ingest`. Failed attempts are limited to 10 per client address per 60 seconds. |
| Local authority unchanged; mirror is one-way | The receiver has **no** write path back into the hub — it never calls the hub, holds no hub credentials, and is not on the hub's request path. The shipper only reads `event_log` (via `events.query_events`, the same read API `event_log_query`'s tool surface uses) and never writes to it. |

## Known PoC limitations (named, not silently shipped around)

- **`kind == "health"` records never carry a status.** There is no dedicated
  agent-heartbeat channel in `event_log` at all. Agent liveness in this
  codebase is tracked in the `registry` table (`registry.touch_agent`), not
  published as an event. `agent.health.alert` (executor-kind activity) is
  the closest available proxy and is what's mirrored; `GET /agents/health`
  on the receiver reports last-seen-per-agent computed from whatever
  allowlisted channel that agent last touched, not a dedicated heartbeat
  signal.
- **Task/approval `status` is "as of ship time", not "as of this event".**
  See "Status: where each kind's comes from" above. Two `agent.task.updated`
  records for the same task in one shipped batch carry the same (current)
  status rather than each event's own historical status — this mirror
  answers "what state is this task in right now" more naturally than "what
  happened at this instant in time" for task/approval kinds specifically.
- **`GET /reviews/pending` is a state query, not an event query.** It takes
  the *latest* record per `(kind, task_id)` — `event_id` (a ULID) sorts
  lexicographically by time, so `MAX(event_id)` within a group is that
  entity's current state — and returns it only if that latest status is
  `waiting_review` (tasks) or `pending`/`requested` (approvals). A task or
  approval that passed through a pending-shaped status but has since moved on
  does not show up, however many historical events for it were ingested.

## Not activated

Current live state: the connector and shipper remain disabled, and no receiver
has been deployed. These steps are still required before the mirror can be
accepted:

- `UA_CLOUD_MIRROR_ENABLED` is not set anywhere live; `run_once()` is a no-op
  without it.
- `deploy/systemd/ua-cloud-mirror.{service,timer}` and
  `deploy/mac-hub/com.unified-agent.cloud-mirror.plist` are example files
  only — not copied into `~/.config/systemd/user/`, not
  `launchctl bootstrap`-ed, not referenced by any install script.
- No receiver has been deployed anywhere (see the deployment steps below).
- The launchd and systemd unit files are templates. Replace the documented
  checkout/UA_HOME placeholders before installing; they are not ready-to-load
  service definitions.

## Deployment

The receiver is a small, stateless-except-SQLite ASGI app with bursty,
low-volume traffic (one batch POST per shipper run, occasional reads). Two
realistic hosts:

- **Cloud Run**: scale-to-zero fits the traffic shape, but its container-local
  filesystem does not persist across scale-to-zero. GCS FUSE does not provide
  the locking guarantees required for SQLite; a durable Cloud Run deployment
  would need a different database such as Cloud SQL/Postgres.
- **Dedicated GCE VM**: selected for this SQLite PoC because a persistent
  standard disk provides durable local storage. Use a small e2-micro in a
  supported Free Tier region when eligible, with a private IAP SSH forward
  from the Mac shipper. Keep it separate from the existing worker.

The receiver is a low-volume proof of concept. Keep the VM's external IP absent,
leave application ingress closed, and check the actual billing account usage
before extending operation beyond the acceptance window.

A rough `Dockerfile` shape for that future deployment (not built, not
pushed):

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY src/unified_agent ./unified_agent
ENV UA_CLOUD_MIRROR_RECEIVER_DB=/data/cloud-mirror-receiver.db
CMD ["uv", "run", "python", "-m", "unified_agent.cloud_mirror_receiver"]
```

The module's `main()` reads `UA_CLOUD_MIRROR_RECEIVER_DB` and calls
`create_app(db_path=...)`. Set `UA_CLOUD_MIRROR_INGEST_TOKEN` and
`UA_CLOUD_MIRROR_RECEIVER_TOKEN` to different high-entropy values. The
database path must remain below `UA_HOME`; the receiver creates private
directories and a private SQLite file there. The shipper accepts HTTPS URLs,
or the exact literal loopback origin `http://127.0.0.1` for a local authenticated
tunnel. Other HTTP destinations (including `localhost` and other loopback
addresses), credentials, query strings, and fragments are rejected. The
bearer token remains mandatory at the receiver; it is sent in the
Authorization header, never in the URL. Loopback HTTP clients ignore proxy
environment variables so the bearer token and records cannot be routed to an
`HTTP_PROXY`; HTTPS clients retain their existing environment behavior.
Failed-auth throttling is a
best-effort per-process guard, so an internet-facing deployment should also
use its platform's edge rate limiter. The local FileSink creates missing
directories with mode 0700 and new JSONL files with mode 0600; it leaves
existing directory modes unchanged, and `UA_CLOUD_MIRROR_FILE_PATH` must
remain below `UA_HOME`.

### Private GCE receiver through an IAP SSH local forward

For a receiver with durable SQLite storage, use a dedicated small VM with a
persistent standard disk. Keep the VM without an external IP and do not open
the application port in the VPC firewall. Bind the receiver to loopback with
`UA_CLOUD_MIRROR_RECEIVER_HOST=127.0.0.1`; the default remains `0.0.0.0` for
container platforms such as Cloud Run. Allow IAP TCP forwarding to SSH only
from Google's documented IAP range `35.235.240.0/20` on port 22. No firewall
rule is needed for the receiver's loopback port.

On the Mac shipper, an authenticated IAP SSH local forward can map a local
loopback port to the receiver's loopback port:

```sh
gcloud compute ssh RECEIVER_VM \
  --project=PROJECT_ID --zone=ZONE --tunnel-through-iap -- \
  -N -L 127.0.0.1:8765:127.0.0.1:8080 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3
```

Run that foreground command under an operator-managed, restartable LaunchAgent
only after verifying the active Mac gcloud identity has IAP tunnel and SSH
permissions. Use `UA_CLOUD_MIRROR_URL=http://127.0.0.1:8765/ingest`; the
shipper will send its bearer token through the forwarded connection. Restrict
the cloud-mirror LaunchAgent plist containing that token to the Mac user
(`chmod 600`). Keep tunnel output and shipper logs free of token values.

IAP carries the TCP stream over an authenticated encrypted tunnel, while the
two HTTP endpoints are confined to loopback. This is why the only cleartext
HTTP URL accepted by `HttpSink` is literal `127.0.0.1`; do not configure an
HTTP URL to a hostname, VPC address, or public address. The SSH identity must
be authorized for the target instance, and IAP TCP forwarding requires
`iap.tunnelInstances.accessViaIAP` plus the documented Compute Engine read
permissions. A service account with only object-storage access is not enough.

This is a deployment runbook, not a deployment. The PR does not create a VM,
firewall rule, persistent disk, LaunchAgent, or receiver process. The live
deployment still needs a Mac identity authorized for IAP/SSH, a confirmed
project billing arrangement, a separately managed pair of receiver tokens,
and successful end-to-end acceptance before the connector is enabled.

## Open questions

- Should `agent.fleet.control` (fleet start/stop/restart events,
  `fleet_actions.py`) be added to the allowlist? It's structurally similar to
  the channels already included, but its `detail` field is an arbitrary
  free-form dict at some call sites, so it would need its own dedicated
  projector rather than reuse of `_mirrored_kind_fields`.
- Is a receiver-side `Content-Length`/media-type check needed beyond the
  existing `max_body_bytes` streaming cap, for a real internet-facing Cloud
  Run deployment sitting behind no other reverse proxy?
- The HTTP sink currently sends one batch per `run_once()` call with no
  built-in backoff/jitter across repeated failures; fine for a 15-minute
  timer PoC, worth revisiting before any real always-on deployment.
