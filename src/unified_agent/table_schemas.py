"""Table definitions for self-contained domain tables, owned by the storage layer.

``storage.init_db()`` creates every table the hub uses. The DDL for these
domain tables lives here, next to storage, so that ``init_db`` never imports
the domain modules above it. Each domain module's ``ensure_schema(conn)``
delegates here, so callers that create one module's tables still can.

Every statement is idempotent (``CREATE ... IF NOT EXISTS``, or an additive
``ALTER``/index change guarded by a ``PRAGMA`` check) and touches only its own
module's tables, so running one is safe at any point of ``init_db``; storage
still calls them at the same points as before.

Schema only. Row migrations and backfills that need a module above core run
after ``init_db`` as post-init hooks (``storage.register_post_init_hook``,
registered by ``post_init``).
"""

from __future__ import annotations

import re
import sqlite3

WORKFLOW_ROLES = """
CREATE TABLE IF NOT EXISTS workflow_instances (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL,
    inputs TEXT NOT NULL, bindings TEXT NOT NULL, outputs TEXT NOT NULL,
    step_index INTEGER NOT NULL, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    status TEXT NOT NULL, execution TEXT NOT NULL, receipt TEXT,
    creator TEXT NOT NULL, org_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_task_steps (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL REFERENCES workflow_instances(id), step_index INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_closures (
    id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES workflow_instances(id),
    task_id INTEGER NOT NULL, actor TEXT NOT NULL, outcome TEXT NOT NULL,
    reason TEXT NOT NULL, target TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_workflow_instances_org_id ON workflow_instances(org_id);
"""

TS_CURSORS = """
-- Task-space (Discord/Telegram) worker cursors (task #770). Same contract as
-- zulip_cursors: last durably-handled message id per surface/channel, advanced
-- only after the event-log write succeeds.
CREATE TABLE IF NOT EXISTS ts_cursors (
  surface TEXT NOT NULL,
  channel TEXT NOT NULL,
  last_message_id INTEGER NOT NULL CHECK (last_message_id >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (surface, channel)
);
CREATE INDEX IF NOT EXISTS idx_ts_cursors_updated ON ts_cursors(updated_at);
"""


def governed_servers(conn: sqlite3.Connection) -> None:
    """Add nullable registry ownership without adopting any legacy registration (#2513)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(governed_servers)")}
    if "org_id" not in columns:
        conn.execute("ALTER TABLE governed_servers ADD COLUMN org_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_governed_servers_org_id ON governed_servers(org_id)"
    )


def routines(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS routines (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          notes TEXT,
          trigger TEXT NOT NULL CHECK(trigger IN ('cron','api','webhook')),
          schedule TEXT,
          lane TEXT NOT NULL,
          project TEXT NOT NULL,
          domain TEXT NOT NULL,
          untrusted INTEGER NOT NULL DEFAULT 0,
          concurrency TEXT NOT NULL CHECK(concurrency IN ('skip','queue')),
          catch_up TEXT NOT NULL CHECK(catch_up IN ('none','last','all')),
          paused INTEGER NOT NULL DEFAULT 0,
          cursor_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          org_id TEXT,
          activity_gate TEXT NOT NULL DEFAULT 'always'
            CHECK(activity_gate IN ('always','require_activity')),
          activity_gate_scope TEXT NOT NULL DEFAULT 'org'
            CHECK(activity_gate_scope IN ('org','project')),
          CHECK ((trigger='cron' AND schedule IS NOT NULL) OR
                 (trigger!='cron' AND schedule IS NULL))
        );
        CREATE TABLE IF NOT EXISTS routine_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
          fire_key TEXT NOT NULL,
          scheduled_for TEXT NOT NULL,
          status TEXT NOT NULL
            CHECK(status IN ('created','queued','skipped','suppressed')),
          task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          reason TEXT,
          window_start TEXT,
          gate_matched INTEGER,
          subject_key TEXT,
          UNIQUE(routine_id, fire_key)
        );
        CREATE INDEX IF NOT EXISTS idx_routine_runs_task ON routine_runs(task_id);
        CREATE INDEX IF NOT EXISTS idx_routines_org_id ON routines(org_id);
        """
    )
    _routine_activity_gate(conn)


# Activity gate (task #1752): columns added in place on tables created before
# them. Values are validated by routine_add; the CHECKs mirror a fresh table.
_ROUTINE_GATE_COLUMNS = (
    (
        "activity_gate",
        "TEXT NOT NULL DEFAULT 'always' CHECK(activity_gate IN ('always','require_activity'))",
    ),
    (
        "activity_gate_scope",
        "TEXT NOT NULL DEFAULT 'org' CHECK(activity_gate_scope IN ('org','project'))",
    ),
)
_ROUTINE_RUN_GATE_COLUMNS = (
    ("reason", "TEXT"),
    ("window_start", "TEXT"),
    ("gate_matched", "INTEGER"),
    ("subject_key", "TEXT"),
)
_RUN_STATUS_CHECK = re.compile(r"(status\s+IN\s*\()([^)]*)(\))", re.IGNORECASE)
_RUN_TABLE_NAME = re.compile(
    r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?routine_runs["`]?', re.IGNORECASE
)


def _routine_gate_pending(conn: sqlite3.Connection) -> bool:
    routine_cols = {row[1] for row in conn.execute("PRAGMA table_info(routines)")}
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(routine_runs)")}
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='routine_runs'"
    ).fetchone()
    return (
        any(name not in routine_cols for name, _ in _ROUTINE_GATE_COLUMNS)
        or any(name not in run_cols for name, _ in _ROUTINE_RUN_GATE_COLUMNS)
        or "'suppressed'" not in ((row[0] or "") if row else "")
    )


def _routine_activity_gate(conn: sqlite3.Connection) -> None:
    """Add the activity-gate columns and the ``suppressed`` run status (#1752).

    Additive columns use guarded ``ALTER``s. SQLite cannot ALTER a CHECK, so
    ``routine_runs`` is rebuilt once from its *stored* SQL with only the status
    value list widened: every column, constraint and index it has today is
    carried over without a hand-copied column list, so no SCHEMA_VERSION bump.
    The check repeats under ``BEGIN IMMEDIATE`` so racing initializers rebuild
    at most once.
    """
    if not _routine_gate_pending(conn):
        return
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # DROP TABLE must not fire ON DELETE actions; the pragma only changes
    # outside a transaction.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            routine_cols = {row[1] for row in conn.execute("PRAGMA table_info(routines)")}
            for name, definition in _ROUTINE_GATE_COLUMNS:
                if name not in routine_cols:
                    conn.execute(f"ALTER TABLE routines ADD COLUMN {name} {definition}")
            run_cols = {row[1] for row in conn.execute("PRAGMA table_info(routine_runs)")}
            for name, definition in _ROUTINE_RUN_GATE_COLUMNS:
                if name not in run_cols:
                    conn.execute(f"ALTER TABLE routine_runs ADD COLUMN {name} {definition}")
            _widen_routine_run_status(conn)
            if "subject_key" not in run_cols:
                # Once, with the column that marks the ledger (#2396): every old
                # skip was an overlap, and the old quiet reason is now no_signal.
                conn.execute(
                    "UPDATE routine_runs SET reason='overlap' "
                    "WHERE status='skipped' AND reason IS NULL"
                )
                conn.execute(
                    "UPDATE routine_runs SET reason='no_signal' WHERE reason='no_activity'"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")


def _widen_routine_run_status(conn: sqlite3.Connection) -> None:
    old_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='routine_runs'"
    ).fetchone()[0]
    if "'suppressed'" in old_sql:
        return
    if len(_RUN_STATUS_CHECK.findall(old_sql)) != 1 or not _RUN_TABLE_NAME.match(old_sql):
        raise sqlite3.DatabaseError("routine_runs has an unrecognised status CHECK")
    new_sql = _RUN_STATUS_CHECK.sub(
        lambda m: m.group(1) + m.group(2).rstrip() + ",'suppressed'" + m.group(3),
        old_sql,
        count=1,
    )
    new_sql = _RUN_TABLE_NAME.sub("CREATE TABLE routine_runs__rebuild", new_sql, count=1)
    columns = ", ".join(row[1] for row in conn.execute("PRAGMA table_info(routine_runs)"))
    extras = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='routine_runs' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY rowid"
        )
    ]
    # Keep AUTOINCREMENT's high-water mark so a deleted run id is never reused.
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='routine_runs'").fetchone()
    conn.execute("DROP TABLE IF EXISTS routine_runs__rebuild")
    conn.execute(new_sql)
    conn.execute(
        f"INSERT INTO routine_runs__rebuild ({columns}) "
        f"SELECT {columns} FROM routine_runs ORDER BY id"
    )
    conn.execute("DROP TABLE routine_runs")
    # Legacy rename semantics: a modern RENAME re-parses every view and trigger
    # in the schema, and one that mentions routine_runs (dropped a line above)
    # would fail it and stop the hub from starting.
    legacy = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE routine_runs__rebuild RENAME TO routine_runs")
    finally:
        conn.execute(f"PRAGMA legacy_alter_table={int(legacy)}")
    for statement in extras:
        conn.execute(statement)
    if seq is not None:
        conn.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='routine_runs'", (seq[0],)
        )
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) SELECT 'routine_runs', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='routine_runs')",
            (seq[0],),
        )


def goals(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS goals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          statement TEXT NOT NULL DEFAULT '',
          parent_goal_id INTEGER,
          project TEXT NOT NULL DEFAULT 'unified-agent',
          status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open','achieved','abandoned')),
          created_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_goal_id);
        CREATE TABLE IF NOT EXISTS task_goal_links (
          task_id INTEGER PRIMARY KEY,
          goal_id INTEGER NOT NULL,
          linked_by TEXT,
          linked_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_goal_links_goal ON task_goal_links(goal_id);
        CREATE TRIGGER IF NOT EXISTS task_goal_links_on_task_delete
        AFTER DELETE ON tasks
        FOR EACH ROW
        BEGIN
          DELETE FROM task_goal_links WHERE task_id = OLD.id;
        END;
        """
    )


def task_comments(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          parent_comment_id INTEGER,
          author TEXT NOT NULL,
          body TEXT NOT NULL,
          mentions TEXT NOT NULL DEFAULT '[]',
          refs TEXT NOT NULL DEFAULT '{}',
          idempotency_key TEXT,
          thread_revision INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id, id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_comments_idem
          ON task_comments(task_id, author, idempotency_key)
          WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS task_comment_threads (
          task_id INTEGER PRIMARY KEY,
          revision INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS task_comments_on_task_delete
        AFTER DELETE ON tasks
        FOR EACH ROW
        BEGIN
          DELETE FROM task_comments WHERE task_id = OLD.id;
          DELETE FROM task_comment_threads WHERE task_id = OLD.id;
        END;
        """
    )


def project_bindings(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_repo_bindings (
          project TEXT PRIMARY KEY,
          repo TEXT NOT NULL,
          default_branch TEXT,
          bound_by TEXT NOT NULL,
          bound_at TEXT NOT NULL
        );
        """
    )


# Ranked task search (task #1419): task_fts and task_embeddings with their
# sync triggers. The backfill of rows that predate task_fts is the
# ``task_search.task_fts`` post-init hook.
TASK_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS task_fts USING fts5(
  title, notes, content='tasks', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS task_fts_ai AFTER INSERT ON tasks BEGIN
  INSERT INTO task_fts(rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS task_fts_ad AFTER DELETE ON tasks BEGIN
  INSERT INTO task_fts(task_fts, rowid, title, notes)
    VALUES ('delete', old.id, old.title, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS task_fts_au AFTER UPDATE OF title, notes ON tasks BEGIN
  INSERT INTO task_fts(task_fts, rowid, title, notes)
    VALUES ('delete', old.id, old.title, old.notes);
  INSERT INTO task_fts(rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;

CREATE TABLE IF NOT EXISTS task_embeddings (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  embedding BLOB NOT NULL
);

-- A cached vector describes the text it was computed from; drop it when that
-- text changes (or the task goes) so the next query re-embeds it lazily.
CREATE TRIGGER IF NOT EXISTS task_embeddings_au AFTER UPDATE OF title, notes ON tasks BEGIN
  DELETE FROM task_embeddings WHERE task_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS task_embeddings_ad AFTER DELETE ON tasks BEGIN
  DELETE FROM task_embeddings WHERE task_id = old.id;
END;
"""


def task_search(conn: sqlite3.Connection) -> None:
    conn.executescript(TASK_FTS)


#: The autonomy tables keyed by mission id, each with a tenant column (#1978).
AUTONOMY_MISSION_TABLES = (
    "autonomy_mission_mode",
    "autonomy_mode_revisions",
    "autonomy_pending_actions",
    "autonomy_standing_grants",
)


def autonomy_modes(conn: sqlite3.Connection) -> None:
    """Mission autonomy tables (#1457, #1544) and their additive upgrades.

    The legacy JSON standing-grant migration is data, not schema: it is the
    ``autonomy_modes.legacy_grants`` post-init hook.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS autonomy_mission_mode (
          mission_id INTEGER PRIMARY KEY,
          mode TEXT NOT NULL,
          standing_grants TEXT NOT NULL DEFAULT '[]',
          updated_at TEXT NOT NULL,
          updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS autonomy_mode_revisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mission_id INTEGER NOT NULL,
          from_mode TEXT,
          to_mode TEXT NOT NULL,
          actor TEXT NOT NULL,
          attribution TEXT NOT NULL,
          reason TEXT,
          approval_id INTEGER,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomy_mode_revisions_mission
          ON autonomy_mode_revisions(mission_id, id);
        CREATE TABLE IF NOT EXISTS autonomy_pending_actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mission_id INTEGER NOT NULL,
          action_class TEXT NOT NULL,
          actor TEXT NOT NULL,
          decision TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          attribution TEXT NOT NULL,
          started INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomy_pending_actions_mission
          ON autonomy_pending_actions(mission_id, started);
        -- Standing grants, normalized (task #1457 Devin BUG finding: a JSON
        -- list column on autonomy_mission_mode required a read-modify-write
        -- that concurrent grants could race and silently drop each other's
        -- exemption). A grant/revoke is now a single atomic INSERT/DELETE.
        CREATE TABLE IF NOT EXISTS autonomy_standing_grants (
          mission_id INTEGER NOT NULL,
          action_class TEXT NOT NULL,
          granted_by TEXT NOT NULL,
          granted_at TEXT NOT NULL,
          PRIMARY KEY (mission_id, action_class)
        );
        CREATE TABLE IF NOT EXISTS autonomy_action_reviews (
          pending_id INTEGER PRIMARY KEY,
          reviewed_by TEXT NOT NULL,
          reviewed_at TEXT NOT NULL,
          approval_id INTEGER
        );
        """
    )
    # Serialize initialization migrations so concurrent startup connections
    # cannot race an ALTER TABLE or index replacement.
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(autonomy_pending_actions)")
        }
        if "action_context" not in columns:
            conn.execute(
                "ALTER TABLE autonomy_pending_actions ADD COLUMN action_context TEXT NOT NULL DEFAULT 'null'"
            )
        index_columns = [
            row["name"]
            for row in conn.execute("PRAGMA index_info(uq_autonomy_pending_actions_open)")
        ]
        if index_columns != ["mission_id", "action_class", "actor", "action_context"]:
            conn.execute("DROP INDEX IF EXISTS uq_autonomy_pending_actions_open")
            conn.execute(
                "CREATE UNIQUE INDEX uq_autonomy_pending_actions_open "
                "ON autonomy_pending_actions(mission_id,action_class,actor,action_context) WHERE started=0"
            )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS autonomy_pending_context_immutable "
            "BEFORE UPDATE OF action_context ON autonomy_pending_actions "
            "WHEN NEW.action_context != OLD.action_context BEGIN "
            "SELECT RAISE(ABORT, 'autonomy action context is immutable'); END"
        )
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(autonomy_action_reviews)")
        }
        if "approval_id" not in columns:
            conn.execute("ALTER TABLE autonomy_action_reviews ADD COLUMN approval_id INTEGER")
        # Tenant column for the mission tables (task #1978): nullable, stamped at
        # insert from edition.tenant_org_id(), so the private hub keeps NULL.
        # Additive, so no SCHEMA_VERSION bump. Reviews are keyed by a pending
        # action id and fenced through that row.
        for table in AUTONOMY_MISSION_TABLES:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "org_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN org_id TEXT")
            # Partition index (#1433, task #2326); additive.
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_org_id ON {table}(org_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def skills_registry(conn: sqlite3.Connection) -> None:
    """Skill versions and assignments (#1353) with their package columns and
    immutability triggers. All schema, no row rewrites."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS skill_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          version INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          source TEXT NOT NULL,
          content TEXT NOT NULL,
          published_by TEXT,
          published_at TEXT NOT NULL,
          UNIQUE(name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_skill_versions_name ON skill_versions(name);
        CREATE INDEX IF NOT EXISTS idx_skill_versions_name_hash
          ON skill_versions(name, content_hash);
        CREATE TABLE IF NOT EXISTS skill_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          skill_name TEXT NOT NULL,
          target_type TEXT NOT NULL CHECK(target_type IN ('agent','profile')),
          target TEXT NOT NULL,
          pinned_version INTEGER,
          assigned_by TEXT,
          assigned_at TEXT NOT NULL,
          UNIQUE(skill_name, target_type, target)
        );
        CREATE INDEX IF NOT EXISTS idx_skill_assignments_target
          ON skill_assignments(target_type, target);
        -- Git-pinned skill sources (#1750). pinned_sha is the commit imports
        -- read; refreshed_sha is what the ref last resolved to, adopted only
        -- by an explicit import at that SHA.
        CREATE TABLE IF NOT EXISTS skill_sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL,
          ref TEXT NOT NULL,
          pinned_sha TEXT NOT NULL
            CHECK(length(pinned_sha) = 40 AND pinned_sha NOT GLOB '*[^0-9a-f]*'),
          refreshed_sha TEXT
            CHECK(refreshed_sha IS NULL
                  OR (length(refreshed_sha) = 40 AND refreshed_sha NOT GLOB '*[^0-9a-f]*')),
          last_refreshed TEXT,
          added_by TEXT,
          added_at TEXT NOT NULL,
          UNIQUE(url, ref)
        );
        """
    )

    # Serialize additive startup changes across a hub/CLI initialization race.
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(skill_versions)")}
        # Never infer attestation from the mutable source of a legacy version.
        for column in ("package_manifest", "package_payload"):
            if column not in columns:
                conn.execute(f"ALTER TABLE skill_versions ADD COLUMN {column} TEXT")
        for operation in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS skill_versions_immutable_{operation.lower()} "
                f"BEFORE {operation} ON skill_versions BEGIN "
                "SELECT RAISE(ABORT, 'skill versions and provenance are immutable'); END"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


#: Holds a row only inside a sanctioned ledger-maintenance transaction
#: (``events.ledger_maintenance``, used by ``events.prune_events``).
EVENT_LOG_MAINTENANCE_TABLE = "event_log_maintenance"


def event_log_append_only(conn: sqlite3.Connection) -> None:
    """Refuse UPDATE and DELETE on ``event_log`` (task #1908). Idempotent.

    The hash chain detects a rewrite after the fact; these triggers stop the
    casual one (a stray ``UPDATE``/``DELETE`` from a script or a SQLite shell)
    before it lands. The one sanctioned rewrite, ``events.prune_events``,
    inserts a row into ``event_log_maintenance`` inside its own write
    transaction and deletes it before commit, so the bypass is never visible
    to another connection and never outlives the transaction (a rollback
    discards it too). Anyone with raw write access to the file can still drop
    the triggers: they are a guard rail, the chain and its external anchors are
    the detection.

    ``execute``, not ``executescript``: this runs inside ``init_db``'s
    transaction and must not commit it.
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {EVENT_LOG_MAINTENANCE_TABLE} ("
        "reason TEXT NOT NULL, started_at TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS event_log_append_only_{operation.lower()} "
            f"BEFORE {operation} ON event_log "
            f"WHEN NOT EXISTS (SELECT 1 FROM {EVENT_LOG_MAINTENANCE_TABLE}) BEGIN "
            "SELECT RAISE(ABORT, 'event_log is append-only; only prune_events may rewrite it'); "
            "END"
        )


def run_usage_heartbeats(conn: sqlite3.Connection) -> None:
    """Heartbeat usage for open self-pickup runs (#1681). Idempotent.

    ``execute``, not ``executescript``: this runs inside ``init_db``'s
    transaction and must not commit it.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS run_usage_heartbeats ("
        "run_id TEXT PRIMARY KEY, "
        "task_id INTEGER NOT NULL, "
        "agent TEXT NOT NULL, "
        "usage_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )


def supervisor_cursors(conn: sqlite3.Connection) -> None:
    """First-mate supervisor inbox cursors (#1918). Idempotent.

    One row per consumer: the ``event_log`` ROWID (``seq``) it has handled
    through. Self-contained, no SCHEMA_VERSION bump. ``execute``, not
    ``executescript``: this runs inside ``init_db``'s transaction.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS supervisor_cursors ("
        "consumer TEXT PRIMARY KEY, "
        "after_seq INTEGER NOT NULL CHECK (after_seq >= 0), "
        "updated_at TEXT NOT NULL)"
    )


def task_liveness(conn: sqlite3.Connection) -> None:
    """One-shot monitors and declared waits per task (#1749). Idempotent.

    One row per task, written by ``task_update(monitor_at=..., declared_wait=...)``
    and read by the liveness sweep. ``monitor_at`` is cleared when the monitor
    fires; ``monitor_attempts`` and ``monitor_fired_*`` keep the record that it
    did, so a fired monitor never fires again. ``wait_first_declared_at`` is the
    start of an unbroken chain of declared waits; a clear or an expiry resets it. Self-contained (no foreign key,
    so no task-table rebuild can touch it), no SCHEMA_VERSION bump. ``execute``,
    not ``executescript``: this runs inside ``init_db``'s transaction.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_liveness ("
        "task_id INTEGER PRIMARY KEY, "
        "monitor_at TEXT, "
        "monitor_note TEXT, "
        "monitor_set_by TEXT, "
        "monitor_attempts INTEGER NOT NULL DEFAULT 0 CHECK (monitor_attempts >= 0), "
        "monitor_fired_at TEXT, "
        "monitor_fired_for TEXT, "
        "monitor_result TEXT, "
        "wait_reason TEXT, "
        "wait_until TEXT, "
        "wait_set_by TEXT, "
        "wait_first_declared_at TEXT, "
        "wait_next_step TEXT, "
        "wait_deploy_target TEXT, "
        "wait_deploy_sha TEXT, "
        "updated_at TEXT NOT NULL)"
    )
    # Additive: a table created before the wait-chain column gets it here.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_liveness)")}
    if "wait_first_declared_at" not in cols:
        conn.execute("ALTER TABLE task_liveness ADD COLUMN wait_first_declared_at TEXT")
    if "wait_next_step" not in cols:
        conn.execute("ALTER TABLE task_liveness ADD COLUMN wait_next_step TEXT")
    for column in ("wait_deploy_target", "wait_deploy_sha"):
        if column not in cols:
            conn.execute(f"ALTER TABLE task_liveness ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_liveness_monitor_at "
        "ON task_liveness(monitor_at) WHERE monitor_at IS NOT NULL"
    )


def task_ownership(conn: sqlite3.Connection) -> None:
    """Stored next-move ownership columns on ``tasks`` (#2388). Idempotent.

    Called after every ``tasks`` rebuild in ``_migrate``, whose hardcoded column
    lists would drop these. The backfill copies the claimant of a ``doing`` row
    and runs only in the migration that adds ``next_owner``, so an owner cleared
    later stays cleared. It leaves ``updated_at`` alone: it is not an edit to
    the task. ``execute``, not ``executescript``: this runs inside ``init_db``'s
    transaction.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    backfill = "next_owner" not in cols
    for name, definition in (
        ("next_owner", "TEXT"),
        ("waiting_on", "TEXT"),
        ("terminal_predicate", "TEXT"),
        ("ownership_generation", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    if backfill:
        conn.execute(
            "UPDATE tasks SET next_owner = claimed_by "
            "WHERE status = 'doing' AND next_owner IS NULL "
            "AND claimed_by IS NOT NULL AND TRIM(claimed_by) <> ''"
        )


def node_followups(conn: sqlite3.Connection) -> None:
    """Operator follow-ups into live ACP node sessions (#1740). Idempotent.

    Self-contained, so no SCHEMA_VERSION bump; its later columns are added
    in place when missing. ``execute``, not ``executescript``:
    this runs inside ``init_db``'s transaction and must not commit it.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_followups (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  mode TEXT NOT NULL,
  body TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  delivered_at TEXT,
  turn_id TEXT,
  path TEXT,
  reason TEXT,
  updated_at TEXT NOT NULL,
  operator_authored INTEGER NOT NULL DEFAULT 0,
  approval TEXT NOT NULL DEFAULT 'gate_passed',
  UNIQUE (item_id, idempotency_key),
  UNIQUE (item_id, seq)
)
"""
    )
    conn.execute(
        """
CREATE INDEX IF NOT EXISTS idx_node_followups_item_status
  ON node_followups(item_id, status, seq)
"""
    )
    # How each follow-up was admitted (task #1872), added in place on a table
    # created before it. Every earlier row passed the node_dispatch gate, so the
    # defaults describe them truthfully.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(node_followups)")}
    if "operator_authored" not in columns:
        conn.execute(
            "ALTER TABLE node_followups ADD COLUMN operator_authored INTEGER NOT NULL DEFAULT 0"
        )
    if "approval" not in columns:
        conn.execute(
            "ALTER TABLE node_followups ADD COLUMN approval TEXT NOT NULL DEFAULT 'gate_passed'"
        )


def node_dispatch_resume(conn: sqlite3.Connection) -> None:
    """Resume bindings for ua-node items (#1741). Idempotent.

    One row per item that reopens an existing harness session instead of
    starting one: the harness session id to resume and where the binding came
    from (``resume`` of an earlier item, or ``adopt`` of a discovered native
    session). Self-contained, with no column edit to ``node_dispatch``, so no
    SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_dispatch_resume (
  item_id TEXT PRIMARY KEY NOT NULL,
  resume_session_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_item_id TEXT,
  agent_session_id TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
)
"""
    )
    conn.execute(
        """
CREATE INDEX IF NOT EXISTS idx_node_dispatch_resume_session
  ON node_dispatch_resume(resume_session_id)
"""
    )


def node_dispatch_cwd(conn: sqlite3.Connection) -> None:
    """The planned real path of a ua-node item's cwd (#1770). Idempotent.

    One row per item whose enqueuer bound the checkout's node-measured real
    path (a team boot does). The node refuses to launch when the cwd no longer
    resolves to it. Self-contained, with no column edit to ``node_dispatch``,
    so no SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_dispatch_cwd (
  item_id TEXT PRIMARY KEY NOT NULL,
  cwd_realpath TEXT NOT NULL,
  created_at TEXT NOT NULL
)
"""
    )


def node_dispatch_wait(conn: sqlite3.Connection) -> None:
    """Items queued for a node that was not live when they were queued (#2110).

    One row per such item: why it is waiting, the heartbeat it was judged on,
    and the deadline after which it expires instead of launching late.
    ``expired_at`` is written in the same transaction that ends the item;
    ``settled_at`` only once its linked run is closed (or there is none), so a
    close that failed, or a crash after the expiry committed, is retried by
    the next sweep. Self-contained, with no column edit to ``node_dispatch``,
    so no SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_dispatch_wait (
  item_id TEXT PRIMARY KEY NOT NULL,
  reason TEXT NOT NULL,
  last_heartbeat TEXT,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expired_at TEXT,
  settled_at TEXT
)
"""
    )


def node_claim_ops(conn: sqlite3.Connection) -> None:
    """Logical ua-node claim operations and their one disposition (#2136). Idempotent.

    A worker persists a claim-operation id before it sends a claim, so a claim
    whose response was lost can be reconciled instead of followed by a fresh
    claim. Each row binds one ``(node, op_id)`` to exactly one queue
    disposition: still ``pending``, ``bound`` to one item attempt with the
    immutable payload it was handed, or terminal ``no_claim``. The unique
    ``(item_id, attempt)`` index keeps one binding per claimed attempt.
    ``fence_expires_at`` (unix seconds) bounds retention; see
    ``node_claim_ops`` for why expiry never reopens admission. Self-contained,
    with no column edit to ``node_dispatch``, so no SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_claim_ops (
  node TEXT NOT NULL,
  op_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  device_node TEXT NOT NULL DEFAULT '',
  issued_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'bound', 'no_claim')),
  reason TEXT,
  item_id TEXT,
  run_id TEXT,
  attempt INTEGER,
  snapshot_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  fence_expires_at INTEGER NOT NULL,
  PRIMARY KEY (node, op_id),
  CHECK ((state = 'bound') = (item_id IS NOT NULL AND attempt IS NOT NULL
    AND snapshot_json IS NOT NULL))
)
"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_node_claim_ops_binding "
        "ON node_claim_ops(item_id, attempt) WHERE item_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_claim_ops_fence ON node_claim_ops(fence_expires_at)"
    )


def dispatch_launch_keys(conn: sqlite3.Connection) -> None:
    """One dispatch run per Control Center launch, enforced by the database (#2501).

    A keyed launch passes an operation key (derived from tenant, identity and
    client key) to ``claim_and_dispatch``, which binds it to the run it mints in
    the same transaction that inserts the run. The key is the primary key, so a
    second hub worker launching under the same key, for any task, is refused by
    the database; only a run that never reached launch (a failed spawn, a refused
    or reclaimed claim) of the same task may be replaced by a retry. The bound run
    is the only launch evidence a launch attempt accepts.

    The row also fences the spawn (review #1521 pass 4): the launcher marks
    ``spawn_started_at`` in a transaction that checks it still holds the claim,
    immediately before spawning, and records ``spawn_outcome`` (``spawned`` or
    ``failed``) when the spawn returns. A run whose spawn started without a
    recorded outcome may still be spawning, so its key is never rebound, until
    ``settle_unsettled_spawn`` proves the launcher (``spawn_launcher``, its
    process token) and the run's job gone (review #1521 pass 5).

    Every dispatch run has a row, keyed or not (#2512): an unkeyed dispatch (the
    scheduler path) files its spawn mark under its per-run key ``run:<run_id>``,
    so one fence covers both and no run of a task with an unsettled spawn is
    reclaimed as retryable or spawned again. A resume of a run fences its spawn
    under its own row, ``run:<run_id>:resume``, re-armed by each resume, so the
    claim's row keeps its outcome (#2527). ``idx_dispatch_launch_keys_run``
    serves those per-run reads. Self-contained, no column edit to
    ``dispatch_runs``, so no SCHEMA_VERSION bump. Rows go with their task
    (``ON DELETE CASCADE``).
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS dispatch_launch_keys (
  launch_key TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  spawn_started_at TEXT,
  spawn_outcome TEXT CHECK (spawn_outcome IN ('spawned', 'failed')),
  spawn_launcher TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_launch_keys)").fetchall()}
    if "spawn_started_at" not in cols:
        conn.execute("ALTER TABLE dispatch_launch_keys ADD COLUMN spawn_started_at TEXT")
    if "spawn_outcome" not in cols:
        conn.execute("ALTER TABLE dispatch_launch_keys ADD COLUMN spawn_outcome TEXT")
    if "spawn_launcher" not in cols:
        conn.execute("ALTER TABLE dispatch_launch_keys ADD COLUMN spawn_launcher TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_launch_keys_task ON dispatch_launch_keys(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_launch_keys_run ON dispatch_launch_keys(run_id)"
    )


def session_credentials(conn: sqlite3.Connection) -> None:
    """Session-scoped hub MCP credentials for node-launched ACP sessions (#1744).

    One row per minted credential. The value itself is never stored: the row is
    keyed by ``sha256(value)``, the same scheme as the OAuth store. At most one
    row per (``item_id``, ``attempt``) is active (``revoked_at IS NULL``); the
    partial unique index holds that even if two mints race. Self-contained, with
    no column edit to an existing table, so no SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS session_credentials (
  credential_id TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  agent_label TEXT NOT NULL,
  device_agent TEXT NOT NULL,
  node TEXT NOT NULL,
  device_node TEXT,
  item_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  task_id INTEGER,
  run_id TEXT,
  acp_session_id TEXT,
  agent_session_id TEXT,
  tools_json TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  org_id TEXT,
  signin_id TEXT
)
"""
    )
    # The device sign-in family a credential was minted under (task #1744
    # review): additive, for a table created by an earlier build of this change.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_credentials)").fetchall()}
    if "signin_id" not in cols:
        conn.execute("ALTER TABLE session_credentials ADD COLUMN signin_id TEXT")
    conn.execute(
        """
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_credentials_active
  ON session_credentials(item_id, attempt) WHERE revoked_at IS NULL
"""
    )
    for column in ("item_id", "task_id", "node", "device_node", "org_id"):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_session_credentials_{column} "
            f"ON session_credentials({column})"
        )


def completion_return(conn: sqlite3.Connection) -> None:
    """Task origins and the completion-return outbox (#1751). Idempotent.

    ``task_origins`` holds the chat thread a task was asked for in, written by
    a gateway from its authenticated request. ``completion_deliveries`` is the
    outbox every status writer appends to, in its own transaction, when a task
    with an origin moves to done, blocked or cancelled. ``(task_id, revision)``
    is the per-transition dedupe key. Both are self-contained (no column edit
    to ``tasks``) and follow a deleted task through the trigger, so not a
    SCHEMA_VERSION bump. ``execute``, not ``executescript``: this runs inside
    ``init_db``'s transaction and must not commit it.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS task_origins (
  task_id INTEGER PRIMARY KEY,
  surface TEXT NOT NULL CHECK (surface IN ('slack', 'zulip', 'messages')),
  channel TEXT NOT NULL,
  thread TEXT NOT NULL,
  requester TEXT NOT NULL,
  created_at TEXT NOT NULL,
  org_id TEXT
)
"""
    )
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS completion_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  revision TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('done', 'blocked', 'cancelled')),
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  deferrals INTEGER NOT NULL DEFAULT 0 CHECK (deferrals >= 0),
  last_error TEXT,
  next_attempt_at TEXT,
  claimed_at TEXT,
  superseded_at TEXT,
  abandoned_at TEXT,
  org_id TEXT,
  UNIQUE (task_id, revision)
)
"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_origins_org_id ON task_origins(org_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_completion_deliveries_org_id "
        "ON completion_deliveries(org_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_completion_deliveries_pending "
        "ON completion_deliveries(task_id) "
        "WHERE delivered_at IS NULL AND superseded_at IS NULL AND abandoned_at IS NULL"
    )
    conn.execute(
        """
CREATE TRIGGER IF NOT EXISTS completion_return_on_task_delete
AFTER DELETE ON tasks
FOR EACH ROW
BEGIN
  DELETE FROM task_origins WHERE task_id = OLD.id;
  DELETE FROM completion_deliveries WHERE task_id = OLD.id;
END
"""
    )


def session_wake(conn: sqlite3.Connection) -> None:
    """Idle-session wake state, one row per session that asked or was granted (#1873).

    Written only by ``messaging.wake``. ``opted_in``/``requested_at`` are the
    request, from the inbox-session registration's ``wake`` flag (never from
    ``agent_sessions.meta``, which is written on insert only, and never from a
    tool). ``granted_at``/``granted_by`` are the human operator's grant (#2050),
    set only through the operator-only wake-grant route. Self-contained, with no
    column edit to ``agent_sessions``, and the grant columns are added in place,
    so no SCHEMA_VERSION bump. Idempotent.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS session_wake (
  session_id TEXT PRIMARY KEY,
  opted_in INTEGER NOT NULL DEFAULT 0,
  transport TEXT NOT NULL DEFAULT 'herdr',
  last_wake_at TEXT,
  last_wake_seq INTEGER NOT NULL DEFAULT 0,
  hour_started_at TEXT,
  wakes_last_hour INTEGER NOT NULL DEFAULT 0,
  unacked_wakes INTEGER NOT NULL DEFAULT 0,
  paused_reason TEXT,
  offered_at TEXT,
  offered_seq INTEGER,
  retry_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_outcome TEXT,
  last_reason TEXT,
  updated_at TEXT NOT NULL,
  requested_at TEXT,
  granted_at TEXT,
  granted_by TEXT
)
"""
    )
    # The operator grant (task #2050), added in place on a table created before it.
    # An earlier row has no grant, so it stops waking until the operator grants it.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_wake)")}
    # The operator's decline of a request (task #2514), bound to the session id like
    # the grant: the session asking again does not clear it; only a grant does.
    for column in (
        "requested_at",
        "granted_at",
        "granted_by",
        "offered_id",
        "declined_at",
        "declined_by",
        "decline_reason",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE session_wake ADD COLUMN {column} TEXT")
    # Existing confirmed deliveries already consumed these budgets. Seed only
    # when adding a field, never overwrite reservations on subsequent startup.
    for column, source in (
        ("last_attempt_at", "last_wake_at"),
        ("attempt_hour_started_at", "hour_started_at"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE session_wake ADD COLUMN {column} TEXT")
            conn.execute(f"UPDATE session_wake SET {column}={source}")
    for column, source in (
        ("attempts_last_hour", "wakes_last_hour"),
        ("unacked_attempts", "unacked_wakes"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE session_wake ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE session_wake SET {column}={source}")
    # Durable generations let a delivered receipt survive a lost connection
    # without consuming a replacement offer for the same mail cursor (#2068).
    conn.execute("""
CREATE TABLE IF NOT EXISTS session_wake_offers (
  offer_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  offered_seq INTEGER NOT NULL,
  offered_at TEXT NOT NULL,
  expires_at REAL NOT NULL,
  invalidated_at TEXT,
  delivered_at REAL,
  recorded_at TEXT,
  outcome TEXT
)
""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_wake_offers_session "
        "ON session_wake_offers(session_id, offered_seq)"
    )
    offer_columns = {row[1] for row in conn.execute("PRAGMA table_info(session_wake_offers)")}
    if "attempted_at" not in offer_columns:
        conn.execute("ALTER TABLE session_wake_offers ADD COLUMN attempted_at TEXT")
    if "reason" not in offer_columns:
        # Task #2391: the node's skip reason, so an unconfirmed wake reads as an
        # unknown append on the message, not as no wake at all.
        conn.execute("ALTER TABLE session_wake_offers ADD COLUMN reason TEXT")


_MEMORY_GRAPH_DDL = (
    """
CREATE TABLE IF NOT EXISTS memory_entities (
  note_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  org_id TEXT,
  PRIMARY KEY (note_id, kind, value)
)
""",
    # Hub check and walk (memory_graph._entity_neighbours): an index range
    # in note_id order, so LIMIT stops the scan without a sort.
    "CREATE INDEX IF NOT EXISTS idx_memory_entities_org_kind_value "
    "ON memory_entities(org_id, kind, value, note_id)",
    """
CREATE TABLE IF NOT EXISTS memory_links (
  src_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  dst_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  rel TEXT NOT NULL
    CHECK (rel IN ('supersedes','contradicts','extends','derives','evidence')),
  actor TEXT,
  created_at TEXT NOT NULL,
  org_id TEXT,
  PRIMARY KEY (src_id, dst_id, rel),
  CHECK (src_id != dst_id)
)
""",
    # Per-seed walks (memory_graph._link_neighbours, note_links) read one
    # end's links in created_at order and stop at their LIMIT.
    "CREATE INDEX IF NOT EXISTS idx_memory_links_org_src_created "
    "ON memory_links(org_id, src_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_links_org_dst_created "
    "ON memory_links(org_id, dst_id, created_at)",
    # The ON DELETE CASCADE from memory(id) looks links up by dst_id (src_id
    # leads the primary key).
    "CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_id)",
    # Superseded by the org-leading indexes above (review round 1).
    "DROP INDEX IF EXISTS idx_memory_entities_lookup",
    "DROP INDEX IF EXISTS idx_memory_links_src",
)

# A row's org_id must be its notes' org_id (task #2096). The write helpers in
# memory_graph already insert through org-fenced joins; these triggers make a
# cross-org edge or entity impossible for any other writer too.
_MEMORY_GRAPH_FENCE = {
    "memory_links_org_fence_insert": (
        "BEFORE INSERT ON memory_links WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.src_id) IS NOT NEW.org_id "
        "OR (SELECT org_id FROM memory WHERE id = NEW.dst_id) IS NOT NEW.org_id"
    ),
    "memory_links_org_fence_update": (
        "BEFORE UPDATE ON memory_links WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.src_id) IS NOT NEW.org_id "
        "OR (SELECT org_id FROM memory WHERE id = NEW.dst_id) IS NOT NEW.org_id"
    ),
    "memory_entities_org_fence_insert": (
        "BEFORE INSERT ON memory_entities WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.note_id) IS NOT NEW.org_id"
    ),
    "memory_entities_org_fence_update": (
        "BEFORE UPDATE ON memory_entities WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.note_id) IS NOT NEW.org_id"
    ),
}


def drop_memory_graph_fence(conn: sqlite3.Connection) -> None:
    """Drop the org-fence triggers, which read ``memory.org_id``.

    For ``storage.downgrade_org_id_columns`` only: SQLite refuses to drop a
    column a trigger references. The next ``init_db`` recreates them.
    """
    for name in _MEMORY_GRAPH_FENCE:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    drop_memory_chunk_fence(conn)


def memory_graph(conn: sqlite3.Connection) -> None:
    """Persisted memory entities and typed links (task #2096, epic #1915 M6).

    ``memory_entities`` holds the identifiers each note names;
    ``memory_links`` holds typed edges between notes. Both cascade on a hard
    delete of either note and carry the note's org_id, enforced by triggers.
    Self-contained tables with no column edit to ``memory``, so no
    SCHEMA_VERSION bump. Rows for older notes are filled by the
    ``tools.memory.graph_backfill`` post-init hook. Idempotent.
    """
    for statement in _MEMORY_GRAPH_DDL:
        conn.execute(statement)
    for name, body in _MEMORY_GRAPH_FENCE.items():
        table = "memory_links" if name.startswith("memory_links") else "memory_entities"
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {name} {body} BEGIN "
            f"SELECT RAISE(ABORT, '{table}: a row must carry its notes'' org_id'); END"
        )


_MEMORY_CHUNK_DDL = """
CREATE TABLE IF NOT EXISTS memory_chunks (
  note_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
  embedding BLOB NOT NULL,
  model TEXT,
  org_id TEXT,
  PRIMARY KEY (note_id, chunk_index)
)
"""

# A chunk's org_id must be its note's org_id, the same fence as memory_entities
# (task #2096). Search also joins memory and filters org_id, which is how
# memory_vec is scoped; the trigger stops any other writer.
_MEMORY_CHUNK_FENCE = {
    "memory_chunks_org_fence_insert": (
        "BEFORE INSERT ON memory_chunks WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.note_id) IS NOT NEW.org_id"
    ),
    "memory_chunks_org_fence_update": (
        "BEFORE UPDATE ON memory_chunks WHEN "
        "(SELECT org_id FROM memory WHERE id = NEW.note_id) IS NOT NEW.org_id"
    ),
}


def drop_memory_chunk_fence(conn: sqlite3.Connection) -> None:
    """Drop chunk org-fence triggers, which read ``memory.org_id``.

    Called from ``drop_memory_graph_fence`` so a downgrade of ``memory.org_id``
    is not blocked by them. The next ``init_db`` recreates them.
    """
    for name in _MEMORY_CHUNK_FENCE:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def memory_chunks(conn: sqlite3.Connection) -> None:
    """Per-note embedding chunks for long notes (task #2182). Idempotent.

    One row per chunk of a note the single 512-token vector cannot cover.
    ``memory_chunk_vec`` (created beside ``memory_vec`` when sqlite-vec loads)
    is the accelerated mirror, keyed by this table's rowid. Both go away with
    the note: the foreign key cascades the rows, and the mirror is deleted
    explicitly first so a reused rowid cannot keep a stale vector (task #1815).
    Self-contained, with no column edit to ``memory``, so no SCHEMA_VERSION bump.
    Older long notes are filled by the ``memory_chunks.backfill`` post-init hook.
    """
    conn.execute(_MEMORY_CHUNK_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_chunks_org_id ON memory_chunks(org_id)")
    for name, body in _MEMORY_CHUNK_FENCE.items():
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {name} {body} BEGIN "
            "SELECT RAISE(ABORT, 'memory_chunks: a row must carry its note''s org_id'); END"
        )


# Prior versions kept per note (task #2097); the capture trigger drops older
# ones. Bounds storage for a note appended to many times, and bounds the read.
MEMORY_VERSIONS_KEPT = 50

_MEMORY_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS memory_versions (
  note_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version >= 1),
  content TEXT NOT NULL,
  tags TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  actor TEXT,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (note_id, version)
)
"""

# Index notes are machine-rendered on every save, so their churn is not kept.
# The prefixes mirror tools.memory.MASTER_INDEX_ID / TAG_INDEX_PREFIX (storage
# may not import a domain module); a test pins the two together. '#' escapes
# the '_' wildcard, as tools.memory._NOT_INDEX_SQL does.
_MEMORY_VERSIONS_TRIGGERS = {
    # Snapshot the row being replaced, attributed to whoever wrote it. BEFORE,
    # so OLD is the stored row; IS NOT, so a no-op write (same text re-saved,
    # or a statement that names the column without changing it) records nothing.
    # updated_at is when the replaced state was written; recorded_at is when it
    # was replaced (the capture time), in storage.now()'s format, so an as_of
    # read shows only states already superseded by then.
    "memory_versions_capture": f"""
CREATE TRIGGER IF NOT EXISTS memory_versions_capture
BEFORE UPDATE OF content, tags ON memory
FOR EACH ROW
WHEN (OLD.content IS NOT NEW.content OR OLD.tags IS NOT NEW.tags)
  AND OLD.id NOT LIKE 'masterindex00%'
  AND OLD.id NOT LIKE 'tagindex#_%' ESCAPE '#'
BEGIN
  INSERT INTO memory_versions (
    note_id, version, content, tags, updated_at, actor, recorded_at
  )
  VALUES (
    OLD.id,
    COALESCE((SELECT MAX(version) FROM memory_versions WHERE note_id = OLD.id), 0) + 1,
    OLD.content,
    OLD.tags,
    OLD.updated_at,
    COALESCE(OLD.updated_by, OLD.author),
    strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
  );
  DELETE FROM memory_versions
  WHERE note_id = OLD.id
    AND version <= (SELECT MAX(version) FROM memory_versions WHERE note_id = OLD.id)
      - {MEMORY_VERSIONS_KEPT};
END
""",
    # A hard delete exists to erase content, so versions go with the note. The
    # foreign key cascades on connections with foreign_keys=ON; this trigger
    # also covers a connection that left it off (the sqlite3 CLI default).
    "memory_versions_on_memory_delete": """
CREATE TRIGGER IF NOT EXISTS memory_versions_on_memory_delete
AFTER DELETE ON memory
FOR EACH ROW
BEGIN
  DELETE FROM memory_versions WHERE note_id = OLD.id;
END
""",
}


def memory_versions(conn: sqlite3.Connection) -> None:
    """Prior versions of each memory note (task #2097, epic #1915 M8). Idempotent.

    ``memory_versions_capture`` copies a note's previous content and tags,
    with the ``updated_at`` and writer of that previous state and the capture
    time ``recorded_at``, whenever an UPDATE changes either, so every edit path (memory_save, memory_delete's
    tombstone, direct SQL) is captured. Version numbers rise
    per note; the newest ``MEMORY_VERSIONS_KEPT`` are kept. Rows go with the
    note on a hard delete. Self-contained: no column edit to ``memory`` and
    no backfill (a note's history starts at its next edit), so no
    SCHEMA_VERSION bump. A trigger whose stored SQL differs from the current
    definition is replaced, so a changed definition deploys.
    """
    conn.execute(_MEMORY_VERSIONS_DDL)
    for name, ddl in _MEMORY_VERSIONS_TRIGGERS.items():
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if stored is not None and stored[0] != stored_trigger_sql(ddl):
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(ddl)


def stored_trigger_sql(ddl: str) -> str:
    """The text SQLite keeps in ``sqlite_master`` for ``ddl``: it drops ``IF NOT EXISTS``."""
    return ddl.strip().replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)


def node_acceptance(conn: sqlite3.Connection) -> None:
    """Device acceptance state, one row per ua-node (task #2112). Idempotent.

    The latest acceptance run for each node: the two harmless items it queued
    (one to complete, one to cancel), the per-check results, and the outcome
    (``pending`` | ``accepted`` | ``failed``). ``agent`` is the node's bound
    agent when the run started, so a registration re-bound to another agent
    is not accepted by an older run. Written only by ``node_acceptance``.
    Self-contained, with no column edit to ``nodes`` or ``node_dispatch``, so
    no SCHEMA_VERSION bump.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_acceptance (
  node TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'failed')),
  complete_item_id TEXT,
  cancel_item_id TEXT,
  checks_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT,
  code_sha TEXT,
  started_by TEXT NOT NULL,
  started_at TEXT NOT NULL,
  evaluated_at TEXT,
  accepted_at TEXT,
  updated_at TEXT NOT NULL
)
"""
    )

    # Additive upgrade for recovered pre-attestation acceptance rows. Old
    # accepted diagnostics remain visible but cannot satisfy the admission gate.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(node_acceptance)")}
    for column in ("attested_by", "attestation_ref", "device_node"):
        if column not in columns:
            conn.execute(f"ALTER TABLE node_acceptance ADD COLUMN {column} TEXT")


def approval_slack_outbox(conn: sqlite3.Connection) -> None:
    """Durable delivery intents for Slack approval cards (#2303). Idempotent.

    One row per card or follow-up, keyed by its Slack dedupe key, so a queued
    delivery survives a hub restart and a lost one is recorded rather than
    vanishing. Self-contained, so no SCHEMA_VERSION bump. ``execute``, not
    ``executescript``: this runs inside ``init_db``'s transaction.
    """
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS approval_slack_outbox (
  dedupe_key TEXT PRIMARY KEY,
  approval_id INTEGER NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('requested','decided')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','sending','done','dead')),
  decision_status TEXT,
  resolved_by TEXT,
  channel_id TEXT,
  ts TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before REAL NOT NULL DEFAULT 0,
  claimed_at REAL,
  created_epoch REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org_id TEXT
)
"""
    )
    conn.execute(
        """
CREATE INDEX IF NOT EXISTS idx_approval_slack_outbox_status
  ON approval_slack_outbox(status, not_before)
"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_slack_outbox_org_id "
        "ON approval_slack_outbox(org_id)"
    )


def task_workflow(conn: sqlite3.Connection) -> None:
    """Per-task stage, resume capsule and graded checks (#2398). Idempotent.

    Self-contained tables, no column edits, so no SCHEMA_VERSION bump.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_workflow ("
        "task_id INTEGER PRIMARY KEY, "
        "org_id TEXT, "
        "stage TEXT, "
        "goal TEXT, "
        "done_json TEXT NOT NULL DEFAULT '[]', "
        "focus TEXT, "
        "head_sha TEXT, "
        "expected_json TEXT NOT NULL DEFAULT '[]', "
        "revision INTEGER NOT NULL DEFAULT 0, "
        "updated_by TEXT, "
        "updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_checks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id INTEGER NOT NULL, "
        "org_id TEXT, "
        "name TEXT NOT NULL, "
        "head_sha TEXT NOT NULL, "
        "outcome TEXT NOT NULL, "
        "grade TEXT NOT NULL, "
        "ref_kind TEXT, "
        "ref TEXT, "
        "repo TEXT, "
        "ref_job TEXT, "
        "detail TEXT, "
        "verification_json TEXT, "
        "recorded_by TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL, "
        "verified_at TEXT, "
        "UNIQUE (task_id, name, head_sha))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS task_checks_task ON task_checks (task_id, recorded_at)"
    )
    # Tenant fences read by org_id first (tenant_tables inventory).
    conn.execute("CREATE INDEX IF NOT EXISTS task_workflow_org ON task_workflow (org_id, task_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS task_checks_org ON task_checks (org_id, task_id, recorded_at)"
    )


def design_panels(conn: sqlite3.Connection) -> None:
    """Independent-first design panels and their sealed seats (#2399). Idempotent.

    Self-contained tables, no column edits, so no SCHEMA_VERSION bump.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS design_panels ("
        "id TEXT PRIMARY KEY, "
        "org_id TEXT, "
        "question TEXT NOT NULL, "
        "context TEXT, "
        "opener TEXT NOT NULL, "
        "task_id INTEGER, "
        "status TEXT NOT NULL, "
        "deadline TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "revealed_at TEXT, "
        "decision TEXT, "
        "rationale TEXT, "
        "considered TEXT, "
        "decided_by TEXT, "
        "decided_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS design_panels_status ON design_panels (status, deadline)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS design_panel_seats ("
        "panel_id TEXT NOT NULL, "
        "seat TEXT NOT NULL, "
        "org_id TEXT, "
        "answer TEXT, "
        "confidence TEXT, "
        "answered_at TEXT, "
        "PRIMARY KEY (panel_id, seat))"
    )
    # Tenant fences read by org_id first (tenant_tables inventory).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS design_panels_org ON design_panels (org_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS design_panel_seats_org ON design_panel_seats (org_id, panel_id)"
    )


def friction(conn: sqlite3.Connection) -> None:
    """Create the friction tables and indexes, idempotently.

    Plain ``conn.execute`` statements (no ``executescript``), so this can run
    inside ``init_db``'s transaction.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS friction_reports ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " org_id TEXT,"
        " key TEXT NOT NULL,"
        " severity TEXT NOT NULL CHECK(severity IN ('low','med','high')),"
        " channel TEXT NOT NULL CHECK(channel IN ('report','session_end','operator')),"
        " reporter TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '',"
        " task_id INTEGER,"
        " created_at TEXT NOT NULL,"
        " source_ref TEXT"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_friction_reports_source "
        "ON friction_reports(channel, source_ref, COALESCE(org_id, '')) "
        "WHERE source_ref IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_friction_reports_key ON friction_reports(key, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_friction_reports_created ON friction_reports(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_friction_reports_org_id ON friction_reports(org_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS friction_verdicts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " key TEXT NOT NULL,"
        " org_id TEXT,"
        " state TEXT NOT NULL CHECK(state IN ('open','fix_claimed','closed','accepted')),"
        " actor TEXT NOT NULL,"
        " note TEXT NOT NULL DEFAULT '',"
        " evidence_ref TEXT,"
        " fix_claimed_at TEXT,"
        " recheck_after TEXT,"
        " closed_at TEXT,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_friction_verdicts_key "
        "ON friction_verdicts(key, COALESCE(org_id, ''))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_friction_verdicts_state "
        "ON friction_verdicts(state, recheck_after)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_friction_verdicts_org_id ON friction_verdicts(org_id)"
    )


# --- helpers --------------------------------------------------------------------
