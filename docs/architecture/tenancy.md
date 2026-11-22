# Table tenancy

Task **#1433**. Status: slice 1 (the inventory and its guard test), slice 2
(task #2326: the org_id indexes and the key constraints) and slice 3 (a
sentinel organization and one storage chokepoint, [off by
default](#the-sentinel-and-the-chokepoint-slice-3)) are in place. `org_id NOT
NULL` comes in a later slice.

## The rule

**A new table must declare its tenancy in
[`src/unified_agent/tenant_tables.py`](../../src/unified_agent/tenant_tables.py).**

`tests/test_tenant_table_inventory.py` fails when any table created under
`src/unified_agent/` is missing from `TABLE_TENANCY`. It also fails if an
entry names a table that no source creates any more, or if a freshly
initialised store holds a table the inventory does not cover.

The organization (`org_id`) is the partition key. Today the column is
nullable and is stamped at write time only on a tenanted enterprise install
(`edition.tenant_org_id()`). A single-org install writes `NULL` and sees no
change. The inventory records which tables are part of that boundary, so no
new table can ship without that decision.

## The four kinds

| Kind | Meaning | What the test checks |
|---|---|---|
| `partitioned` | Rows hold data an organization owns: an agent's, a task's, a user's or a device's. | The table has an `org_id` column and an index whose leading column is `org_id`. |
| `derived` | Each row belongs to one row of a partitioned parent and inherits that row's tenant. Reads reach it through the parent. | The entry names the parent table and the reference column (`via`). The parent chain ends at a partitioned table in the same store. The column exists and is **effectively NOT NULL**: declared `NOT NULL`, an `INTEGER PRIMARY KEY` rowid alias, or a key column of a `WITHOUT ROWID` table. Elsewhere, SQLite lets a `PRIMARY KEY` column hold NULL. |
| `accelerator` | A virtual search table (FTS5 or sqlite-vec) whose rows mirror a tenant-bearing parent one to one by rowid. | The table is virtual (every virtual table must be an accelerator). The parent has the named rowid (`rowid`, or an `INTEGER PRIMARY KEY` alias). For FTS5, `content=` names the parent, `content_rowid` matches `via`, and a trigger on the parent writes to it. A vec0 mirror must be keyed by `rowid`. |
| `global` | No tenant data at all: store metadata, process cursors and locks, and hub telemetry without free text. | The table has no tenant column (`agent`, `task_id`, `query`, `error`, `reason`, `content` and others). Exemptions are listed by name in the test, each with a reason. |

Every entry carries a one-line reason. `store` names the database the table
lives in. Almost all tables are in the hub store (`memory.db`); the cloud
mirror receiver, the pilot datastore, the invocation receipt schema,
the disposable synthetic provider and the LangGraph checkpoint store
(`langgraph_resume_leases`; LangGraph's own checkpoint tables are library-owned)
each have their own.

## Choosing a kind

- Can a row hold anything an agent, task, user or device produced or owns,
  including free text such as an error message or a query? It is
  `partitioned`.
- Does every row point, through an effectively NOT NULL column, at exactly
  one row of a partitioned table? It may be `derived`.
- Is it a virtual FTS or vector index over a partitioned table? It is an
  `accelerator`.
- Is it store metadata, a process lock or cursor, or telemetry with no
  tenant text? Only then is it `global`.
- **Unsure? Choose `partitioned`.** If the table has no `org_id` column, the
  ratchet records the gap. A wrong `global` would hide it.

Create a new `partitioned` table with a nullable `org_id TEXT` column and
`CREATE INDEX ... ON <table>(org_id)`, and stamp it from
`edition.tenant_org_id()` at insert, the way `goals` and `plans` are in
`storage.py`. A new partitioned table cannot join the pending lists.
The [chokepoint](#the-sentinel-and-the-chokepoint-slice-3) picks it up from the
inventory. Open the hub store through `storage.connect` or
`org_partition.open_store`, and compare with `org_id IS ?`, never `=`.

## How tables are found

The static half of the test reads every Python module under
`src/unified_agent/` through its syntax tree. It evaluates each string
expression as far as it can: literals, f-strings, `+`, `%` and
`str.format`, plus string constants at module or function level. Every
`*.sql` file under `src/` is scanned too.

In the text it finds `CREATE [TEMP] [VIRTUAL] TABLE [IF NOT EXISTS] <name>`,
including `CREATE TABLE ... AS SELECT`, and `ALTER TABLE ... RENAME TO
<name>`. Names may be quoted (`"x"`, `'x'`, `` `x` ``, `[x]`) or
schema-qualified (`main.x`).

A statement whose table name cannot be resolved fails the test. So does a
statement whose name is built outside the literal. Use a literal or a module
constant for the name. A genuinely dynamic site goes into `_DYNAMIC_SITES` in
the test, keyed by file and function, with a reason. An allowlist entry whose
site no longer exists also fails.

The runtime half builds each store: the hub store through `init_db`, every
module's `ensure_schema`, and the lazy table creators. It then compares the
real schema with the inventory. Shadow tables are accepted only if they
belong to an inventoried virtual table. FTS5 shadows are recognised by
SQLite's own `PRAGMA table_list` type `shadow`. sqlite-vec does not register
its shadows, so a vec0 shadow is recognised by its exact suffix (`_info`,
`_chunks`, `_rowids`, `_vector_chunksNN`, …) on an existing vec0 table name.

## Exempt tables

- **Rebuild temporaries.** Table-rebuild migrations create transient tables
  (`tasks_new`, `routine_runs__rebuild` and others) and rename them over the
  original. The test lists them by name, never by pattern.
- **Connection temp tables** (`CONNECTION_TEMP_TABLES`). These are scratch
  tables such as `_ua_search_candidates`. Each holds ids of rows the calling
  query already selected and lives only in one connection's temp schema. The
  test fails if any site creates one without `TEMP`.

## The pending ratchets

Two read-only maps hold the known gaps. Each value names the follow-up task.

- `PARTITION_PENDING`: partitioned tables with no `org_id` column yet.
- `ORG_INDEX_PENDING`: partitioned tables that have `org_id` but no index
  leading on it.

Both may only shrink. Each must stay a subset of a frozen baseline
(`PARTITION_PENDING_BASELINE`, `ORG_INDEX_PENDING_BASELINE`), so a new table
cannot join, not even in exchange for one that leaves. The test checks that
every member still lacks what it is pending. Adding the column or the index
therefore forces you to remove the entry.

## Related code

- `edition._LEGACY_TENANT_TABLES` is still maintained by hand. The test
  asserts it is a subset of the partitioned tables that already carry
  `org_id`.
- `storage._add_org_id_columns` and `storage.downgrade_org_id_columns` are the
  tested up and down paths for the original five tenant columns (task #1232).

- `storage.downgrade_org_id_indexes` drops the eighteen partition indexes
  task #2326 added (`storage.ORG_ID_PARTITION_INDEX_TABLES`); the next
  `init_db` recreates them.

## The key-constraint rebuild (task #2326)

`CREATE TABLE IF NOT EXISTS` never changes an existing table, so a constraint
added to a CREATE statement reaches only fresh stores. To keep fresh and
upgraded hubs identical, `constraint_migration` rebuilds an older store once:

- `task_comments.task_id` gains `REFERENCES tasks(id) ON DELETE CASCADE`.
  Comments whose task no longer exists are deleted.
- The text primary keys of `memory_embeddings`, `memory_recall_votes`,
  `node_acceptance`, `ui_session_workos`, `node_dispatch_cwd`,
  `node_dispatch_resume` and `node_dispatch_wait` become `NOT NULL`. Rows with
  a NULL key are deleted.

`init_db` runs it last, after the schema commit. It runs once per store: the
`meta` row `constraint_rebuild_2326` records the run (when, what was rebuilt,
per-table deletions, the backup path) and stops every later attempt. Before
any change, inside the same write transaction, the affected tables are copied
to `UA_BACKUP_DIR/constraints-2326-<UTC stamp>.db`; the copy must pass
`PRAGMA integrity_check` and match the store row for row (count and digest)
before the first rebuild. The rebuilds, a foreign-key check, the marker and
one `agent.storage.constraints_rebuilt` evidence event commit together; any
failure rolls all of it back and startup fails, leaving the store as it was.
A fresh store needs no rebuild and gets only the marker: no backup, no event.

Preview it without changing anything (read-only connection):

```bash
unified-agent db migrate-constraints --dry-run            # active memory.db
unified-agent db migrate-constraints --dry-run --path copy.db --json
```

`unified-agent db migrate-constraints` without `--dry-run` applies it to the
active store now instead of at the next start.

There is no `SCHEMA_VERSION` bump. The rebuilt tables keep every column, so
older code opens the stricter schema unchanged and a rollback deploy needs no
schema step. The rebuild has no in-place reverse: the rows it deleted are
invalid under the new keys and stay in the backup file named by the marker.

## The sentinel and the chokepoint (slice 3)

Until an operator acts, nothing in this section runs. A deploy changes no
stored row, adds no trigger and returns a plain `sqlite3.Connection`.

**The rule: Python keeps seeing NULL, SQLite keeps the sentinel, and the
connection is the boundary.** On a private hub `org_id IS NULL` has always
meant "the single org". About 500 read and write sites pass
`edition.tenant_org_id()` (`None`) to `org_id IS ?`, and branch on that `None`
to decide whether a tenant exists. A backfill would silently empty every one
of them. So none of them change: `org_partition` translates at the connection,
in one place.

The `meta` row `org_partition_1433` decides how a store is read, not the
process environment, so every process sharing a data directory agrees. On a
marked store `storage.connect` returns a `ScopedConnection`:

| Statement | What the chokepoint does |
|---|---|
| `SELECT` | Shadows each scoped table it names with a filtered CTE: other orgs' rows are gone and `org_id` reads as NULL, so every spelling of the old predicate still answers the same. |
| `INSERT ... VALUES` | Stamps the sentinel inside `VALUES`, before SQLite checks unique keys, adding the column if the statement omitted it. |
| `UPDATE`, `DELETE`, upserts | Compares `org_id IS ...` through `NULLIF`, and replaces a table read in a `FROM` or `JOIN` with its filtered rows. |
| Anything else | Passes through unchanged. `executescript` is not rewritten; the triggers still apply. |

Below the connection, the backfill installs four persistent triggers per
scoped table (`ua_org_part_*`). They stamp a row inserted without an org and
abort any write that touches another org's row, so a raw connection cannot
step around the boundary. Hub readers that open the file themselves use
`org_partition.open_store`.

`SCOPED_TABLES` is every partitioned hub table that is neither in
`PARTITION_PENDING` nor in `EXEMPT_TABLES`, so a new partitioned table is
covered with no further step. `tests/test_org_partition.py` is the ratchet. It
fails when a partitioned table is neither scoped nor listed, when a table
holds an `org_id` outside the chokepoint, when a new raw `sqlite3.connect`
appears, or when SQL names `main.<scoped table>`. One parametrized test proves
stamp, hide and fence on every scoped table. Run an existing module against a
partitioned store with `UA_TEST_ORG_PARTITION=1`; application behaviour must
not change. `tests/org_partition_exempt.txt` lists the tests that do not apply
to such a store, with the reason for each group, and conftest deselects them,
so any failure in that run is a defect. A clean full run gates an operator's
live `--apply` (task #2340).

### What relied on NULL meaning the one org

| Pattern | Where | After a backfill |
|---|---|---|
| `org_id IS ?` with `None`, in reads | Almost every module | Unchanged: the CTE shows NULL. |
| `org_id IS ?`, `x.org_id IS excluded.org_id`, in writes | Tenant-fenced writes and upserts | Unchanged: compared through `NULLIF`. |
| `COALESCE(org_id, '')` unique keys | `agent_tokens`, `message_index`, `task_holds` | Still unique: the value is stamped before the check. |
| Memory org fences | Triggers on `memory_links`, `memory_entities`, `memory_chunks` | Still hold: writers copy the stored org. Parked during the backfill. |
| Hub readers on their own connection | `sleep_pass`, `evolve`, `planning_loop`, `shipping_preflight_cli`, the managed-worker check in `config` | Use `open_store`. The last one fails open if it reads nothing, so it is tested directly. |
| `tenant_org_id() is None`, `... or ""` | `managed_secrets`, idempotency keys, handoff keys, `memory_index_dirty` | Unchanged: Python still sees `None`. |
| `event_log` | Append-only, hash-chained | Stamped through the ledger maintenance lift. The hash does not cover `org_id`, so the chain still verifies. |

Three NOT NULL tables (`connector_oauth_pending`,
`connector_oauth_connections`, `llm_broker_spend`) use `''` as the private
hub's scope. They are `EXEMPT_TABLES` and are neither backfilled nor filtered.

### Enabling and rolling back

The sentinel does not enable multi-tenancy. It remains one org on one hub, and
a process that stamps a real org is refused on a partitioned store.

To enable, on the hub:

1. Stop every process that uses the store. A long-lived connection opened
   before the backfill would keep reading `org_id IS NULL`.
2. `unified-agent db org-partition` reports the rows per table and changes
   nothing. Any row that already names an org refuses the run.
3. `UA_ORG_PARTITION=1 unified-agent db org-partition --apply`. Inside one
   transaction it writes a verified copy of every table it will change to
   `UA_BACKUP_DIR/org-partition-1433-<UTC stamp>.db`, stamps the rows, installs
   the triggers, writes the marker and records one
   `agent.storage.org_partition_applied` evidence event. Any failure rolls all
   of it back. A second run does nothing.
4. Start the processes. Add `UA_ORG_PARTITION=1` to no service: the flag only
   permits step 3, and the marker decides everything after it.

To roll back, before deploying any build older than this slice:

1. Stop every process that uses the store.
2. `unified-agent db org-partition --downgrade`. It needs no flag. It removes
   the triggers and the marker and turns every sentinel, including rows written
   since, back into NULL, in one transaction, and records
   `agent.storage.org_partition_downgraded`.
3. Start the processes.

An older build has no chokepoint, so on a marked store its `org_id IS NULL`
reads return nothing. The downgrade must come first. The backup is a copy of
the store before the backfill; the downgrade does not need it.

`init_db` never starts a backfill. On a marked store it stamps and adds
triggers for a table that joined the partition since, without touching the
marker, so a fresh store holds no wall-clock value from this feature.

### What slice 3 leaves

- `org_id NOT NULL`. It needs a rebuild of each scoped table, like
  `constraint_migration`, and a decision for the `''` tables.
- The `PARTITION_PENDING` tables. They carry no `org_id` yet and join the
  chokepoint when they do.
- `derived` tables are not filtered. They are read through a parent today.
- A second org. The connection scopes to the sentinel only. A real org needs a
  per-request scope in place of the constant, and enabling one is a separate
  decision. Postgres is out of scope.
