"""SQLite storage for memory and tasks (single DB, WAL, FTS5 for memory search)."""

import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, edition, org_partition, table_schemas

SCHEMA_VERSION = 43

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_search_cache (
  provider TEXT NOT NULL,
  query TEXT NOT NULL,
  max_results INTEGER NOT NULL,
  results TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (provider, query, max_results)
);

CREATE TABLE IF NOT EXISTS credential_pool (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  label TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('registered', 'revoked')),
  active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credential_pool_status ON credential_pool(status);

-- vault_ingest_ledger (Karpathy-LLM-Wiki-inspired safeguard #2): content-hash
-- ledger for the steward-inbox pre-ingest gate. One row per vault-relative
-- path fed into the Wiki layer, recording the sha256 of its last-ingested
-- content so vault_ingest_status can tell an agent whether a capture is
-- "new", "unchanged", or "changed" since it was last folded into the wiki.
CREATE TABLE IF NOT EXISTS vault_ingest_ledger (
  path TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  agent TEXT,
  pages TEXT NOT NULL DEFAULT '[]',
  reason TEXT
);

CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY,
  title TEXT,
  content TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  author TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  salience REAL NOT NULL DEFAULT 0.5,
  confidence REAL,
  last_accessed TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  source_host TEXT,
  -- When set, this note has been superseded (demoted) in favour of another note.
  -- It points at the surviving note's id. Superseded notes are hidden from
  -- memory_search/memory_list but remain fully retrievable via memory_get and
  -- can be re-promoted (superseded_by cleared) — nothing is ever destroyed.
  superseded_by TEXT,
  -- Optional TTL. Expired notes drop out of default search/list but remain
  -- retrievable by id (task #508). NULL means no expiry.
  expires_at TEXT,
  feedback_helpful INTEGER NOT NULL DEFAULT 0,
  feedback_not_helpful INTEGER NOT NULL DEFAULT 0,
  feedback_stale INTEGER NOT NULL DEFAULT 0,
  feedback_wrong INTEGER NOT NULL DEFAULT 0,
  -- Nullable tenant boundary: task #1232. Stamped only when
  -- edition.tenant_org_id returns a value -- UA_EDITION=enterprise AND an
  -- org is configured. NULL everywhere else including the private hub.
  -- Not enforced or filtered on anywhere yet: the column exists ahead of
  -- pilot #2 needing it.
  -- NOTE for future editors: SQLite's ALTER TABLE DROP COLUMN reparses this
  -- table's stored CREATE TABLE text. A line comment mixing a closing
  -- parenthesis with a comma corrupts that reparse with "incomplete input"
  -- even though the comment is inert SQL. Keep comments near org_id free of
  -- that combination -- see downgrade_org_id_columns's test for the repro.
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory(updated_at);


CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title, content, content='memory', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
END;
-- memory_au (reindex FTS on UPDATE) is created + kept current in _migrate(),
-- scoped to `OF title, content` so hot-path access_count/last_accessed bumps in
-- memory_search/memory_get don't needlessly re-index FTS. See _MEMORY_AU_TRIGGER.

CREATE TABLE IF NOT EXISTS memory_embeddings (
  note_id TEXT PRIMARY KEY NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
  embedding BLOB NOT NULL,
  model TEXT
);

-- idempotency_keys is declared once, in _IDEMPOTENCY_SCHEMA. init_db and
-- _migrate both apply that constant; repeating the CREATE here would be a
-- third copy of the same statement.
-- memory_vec (System Leverage Plan T1-2): accelerated mirror of
-- memory_embeddings for memory_search's semantic step and memory_save's
-- dedup check. A sqlite-vec vec0 virtual table, not a plain table — its
-- creation is NOT in _SCHEMA (executescript would abort the whole batch if
-- the extension failed to load on this connection); see
-- _ensure_memory_vec_table() in init_db(). Keyed by memory.rowid, the same
-- rowid-mirroring pattern memory_fts already uses via content_rowid.
-- memory_embeddings stays the source of truth (consolidate.py and the
-- indexer's analogous table are untouched); this is purely an optional,
-- best-effort accelerated index kept in sync at the same write sites.

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','doing','waiting_review','blocked','done','cancelled')),
  priority TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
  parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  archived_at TEXT,
  source_host TEXT,
  claimed_by TEXT,
  claimed_at TEXT,
  lock_expires_at TEXT,
  untrusted INTEGER NOT NULL DEFAULT 0,
  worktree_path TEXT,
  branch TEXT,
  project TEXT NOT NULL DEFAULT 'unified-agent',
  domain TEXT NOT NULL DEFAULT 'hobby',
  due_at TEXT,
  evidence_ref TEXT,
  lane TEXT NOT NULL DEFAULT 'default' CHECK (length(lane) >= 1 AND length(lane) <= 64),
  -- Nullable tenant boundary (task #1232); see the memory table's org_id
  -- comment above for the gating rule.
  org_id TEXT,
  -- current_run_id / current_run_agent (task #1526): a cheap, indexed cache
  -- of "this task's open self-pickup run", maintained by run_records.py in
  -- the same transaction as the claim/finish event it mirrors. Provider-usage
  -- attribution (usage_ledger.resolve_run_id) reads these two columns
  -- instead of scanning event_log, so it stays O(1) regardless of event_log
  -- size. NULL means no open run this cache knows about; never fabricated.
  current_run_id TEXT,
  current_run_agent TEXT,
  -- cancel_requested (task #1525): scoped to current_run_id — set only while
  -- that run is open, cleared the moment it closes or a new run opens (see
  -- run_records._bind_current_run / _unbind_current_run). 0/1, never NULL, so
  -- a plain WHERE cancel_requested=1 finds every live cancellation request
  -- without a NULL-handling special case.
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  -- Stored next-move ownership (task #2388); task_ownership.py owns the meaning.
  next_owner TEXT,
  waiting_on TEXT,
  terminal_predicate TEXT,
  ownership_generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
-- idx_tasks_project / idx_tasks_domain / idx_tasks_due_at are created in
-- _migrate(), not here: on a pre-existing DB this executescript runs before
-- those columns have been ALTERed in, and CREATE INDEX IF NOT EXISTS on any
-- of them fails outright ("no such column") rather than being the no-op
-- CREATE TABLE IF NOT EXISTS is.


CREATE TABLE IF NOT EXISTS workspace_symbols (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace TEXT NOT NULL,
  file_path TEXT NOT NULL,
  symbol_name TEXT NOT NULL,
  symbol_type TEXT NOT NULL CHECK (symbol_type IN ('class', 'method', 'function')),
  parent_name TEXT,
  signature TEXT,
  docstring TEXT,
  line_no INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspace_symbols_workspace ON workspace_symbols(workspace);
CREATE INDEX IF NOT EXISTS idx_workspace_symbols_name ON workspace_symbols(symbol_name);

CREATE TABLE IF NOT EXISTS workspace_symbol_embeddings (
  symbol_id INTEGER PRIMARY KEY REFERENCES workspace_symbols(id) ON DELETE CASCADE,
  embedding BLOB NOT NULL,
  model TEXT
);

-- Plan-and-reason primitives (task #61): dumb storage for goal decomposition,
-- checkpointing, and bounded self-correction. No LLM in the loop, no auto-execution;
-- the calling agent writes its own decomposition and drives progress. Plans live in
-- the shared DB (cross-agent, persistent) with a `scope` field, mirroring the
-- single-pool decision used for memory.
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','done','blocked','abandoned')),
  author TEXT,
  escalated INTEGER NOT NULL DEFAULT 0,
  escalation_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  step_idx INTEGER NOT NULL,
  description TEXT NOT NULL,
  owner TEXT,
  depends_on TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','doing','done','failed','blocked')),
  note TEXT,
  retries INTEGER NOT NULL DEFAULT 0,
  done_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (plan_id, step_idx)
);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id);

CREATE TABLE IF NOT EXISTS plan_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  context TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (plan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_plan_checkpoints_plan ON plan_checkpoints(plan_id);
"""

# --- agent_events (task 64 phase A) -------------------------------------------
# Append-only activity index for the agent cluster: one row per notable
# mutation (task status change, memory save, journal entry). The vault journal
# stays the canonical human-readable record; this table is the queryable index
# consumed by monitor.py. Kept in its own executescript block — separate from
# _SCHEMA — so sibling branches editing _SCHEMA rebase trivially around it.
_AGENT_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref_id TEXT,
  summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events(ts);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent);
CREATE INDEX IF NOT EXISTS idx_agent_events_kind ON agent_events(kind);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent_kind_ts ON agent_events(agent, kind, ts);

"""


def record_agent_event(
    kind: str,
    ref_id: str | None = None,
    agent: str | None = None,
    summary: str | None = None,
    *,
    mirror: bool = True,
    approval_tool: str | None = None,
    heartbeat: bool = True,
    run_id: str | None = None,
) -> None:
    """Best-effort append of one row to agent_events. Never raises.

    An event write is bookkeeping layered on a primary operation (task update,
    memory save, journal entry) and must never break it — any failure here is
    swallowed. `agent` may be None when no label is derivable at the call
    site. Summaries are truncated to 200 chars. ``approval_tool`` and
    ``run_id`` are optional request metadata for the bus mirror only; neither
    adds an activity column. ``run_id`` is display linkage, not authority.
    Maintenance events attributed to a former actor must set ``heartbeat=False``:
    attribution alone is not evidence that the actor is still working.
    """
    try:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO agent_events (agent, ts, kind, ref_id, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, now(), kind, ref_id, summary[:200] if summary else summary),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    # Mirror onto the swarm event bus (task #157) so every existing call site
    # here becomes a publisher without being edited. Separately guarded: the
    # primary agent_events write above must survive a broken bus, and this
    # function's never-raises contract is not relaxed to make bus errors
    # visible — events.record_event is the API that reports its failures.
    if mirror:
        try:
            from .events import mirror_agent_event

            mirror_agent_event(
                kind,
                ref_id=ref_id,
                agent=agent,
                summary=summary,
                approval_tool=approval_tool,
                run_id=run_id,
            )
        except Exception:
            pass

    # Same trick again for the agent registry: every existing call site here becomes
    # a liveness heartbeat without being edited. Separately guarded and throttled
    # inside touch_agent, so a burst of events collapses to one row update.
    #
    # Note this deliberately does NOT validate `agent` against config.agent_labels().
    # 874 rows of history depend on record_agent_event never validating, and the
    # registry is a mirror of what actually happened — unregistered labels get a row
    # with known=0 and surface in the agent_label_drift doctor check.
    if heartbeat:
        try:
            from .registry import touch_agent

            touch_agent(agent, via="agent_event")
        except Exception:
            pass


# --- end agent_events ----------------------------------------------------------


# --- event_log (task #157: Swarm Event Bus) -------------------------------------
# The append-only, ULID-ordered event store behind events.py. Distinct from
# agent_events, which is a denormalized activity INDEX for monitor.py: this is
# the ordered log consumers resume from by cursor, and the ledger task #156's
# policy engine will fail closed on.
#
# `episode_id` and `causal_parent_id` ship here, nullable and unwritten, on
# purpose. #156 needs them and would otherwise have to rebuild this table on the
# live shared memory.db a week after creating it — the exact shape of the #178
# migration defect. Adding semantics to an existing nullable column is free;
# adding the column is not.
_EVENT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
  ulid TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  originator TEXT,
  episode_id TEXT,
  causal_parent_id TEXT,
  created_at TEXT NOT NULL,
  dedupe_key TEXT,
  prev_hash TEXT,
  entry_hash TEXT,
  -- Nullable tenant boundary (task #1232); see the memory table's org_id
  -- comment in _SCHEMA above for the gating rule.
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_log_channel ON event_log(channel);
CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_episode ON event_log(episode_id);
-- (channel, created_at) serves the tool_allowance ledger count and every
-- other "recent rows of these channels" scan; on a private hub (all org_id
-- NULL) the single-column indexes leave the planner a full-log walk
-- (task #2144).
CREATE INDEX IF NOT EXISTS idx_event_log_channel_created ON event_log(channel, created_at);
"""

# Optional accelerator for node_queue._TURN_EVENTS_SQL. Python's event serializer
# retains NaN/Infinity spellings in the evidence ledger. SQLite before 3.42 cannot
# parse them, so indexing all node payloads there would turn a best-effort summary
# limitation into rejected ledger writes (or prevent startup over existing rows).
_NODE_TURN_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_event_log_node_item_turn ON event_log(
  json_extract(payload, '$.item_id'), json_extract(payload, '$.turn_id')
) WHERE channel = 'agent.node';
"""


def sqlite_runtime_version(conn: sqlite3.Connection) -> str:
    """Read the engine linked to this connection, rather than a package version."""
    return str(conn.execute("SELECT sqlite_version()").fetchone()[0])


def node_turn_index_supported(conn: sqlite3.Connection) -> bool:
    """Can this engine parse the ledger's non-finite JSON spellings?"""
    version = tuple(int(part) for part in sqlite_runtime_version(conn).split("."))
    if version < (3, 42, 0):
        return False
    try:
        # JSON can be omitted at compile time even on a recent SQLite engine.
        conn.execute("SELECT json_extract(?, '$.n')", ('{"n":NaN,"i":Infinity}',)).fetchone()
    except sqlite3.OperationalError:
        return False
    return True


def _migrate_node_turn_index(conn: sqlite3.Connection) -> None:
    if node_turn_index_supported(conn):
        conn.executescript(_NODE_TURN_INDEX_SQL)
    else:
        # A database can move from a newer SQLite engine to an older one without
        # changing schema_version. Remove only this rebuildable accelerator before
        # any event writes; never normalize payloads or change their ledger hashes.
        conn.execute("DROP INDEX IF EXISTS idx_event_log_node_item_turn")


# Zulip polling state is deliberately separate from credentials.  The worker
# stores only the last message id it has durably handled for each bot/channel
# pair; zuliprc files remain owner-only files outside SQLite.
_ZULIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS zulip_cursors (
  agent_label TEXT NOT NULL,
  channel TEXT NOT NULL,
  last_message_id INTEGER NOT NULL CHECK (last_message_id >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (agent_label, channel)
);
CREATE INDEX IF NOT EXISTS idx_zulip_cursors_updated ON zulip_cursors(updated_at);
CREATE TABLE IF NOT EXISTS zulip_worker_health (
  agent_label TEXT NOT NULL,
  channel TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'not_started',
  last_poll_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  messages_processed INTEGER NOT NULL DEFAULT 0,
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (agent_label, channel)
);
CREATE INDEX IF NOT EXISTS idx_zulip_worker_health_status ON zulip_worker_health(status);
CREATE TABLE IF NOT EXISTS zulip_event_queues (
  agent_label TEXT PRIMARY KEY,
  queue_id TEXT,
  last_event_id INTEGER NOT NULL DEFAULT -1 CHECK (last_event_id >= -1),
  registered_at TEXT,
  last_event_at TEXT,
  status TEXT NOT NULL DEFAULT 'not_started',
  last_error TEXT,
  next_retry_at TEXT,
  reset_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zulip_event_queues_status ON zulip_event_queues(status);
CREATE TABLE IF NOT EXISTS zulip_bridge_state (
  name TEXT PRIMARY KEY,
  cursor TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zulip_outbound_notifications (
  dedupe_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  agent_label TEXT NOT NULL,
  channel TEXT NOT NULL,
  topic TEXT NOT NULL,
  status TEXT NOT NULL,
  sent_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zulip_outbound_sent ON zulip_outbound_notifications(sent_at);
CREATE TABLE IF NOT EXISTS zulip_chat_replies (
  inbound_message_id INTEGER PRIMARY KEY,
  agent_label TEXT NOT NULL,
  channel TEXT NOT NULL,
  topic TEXT NOT NULL,
  status TEXT NOT NULL,
  reply_message_id INTEGER,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zulip_chat_replies_status ON zulip_chat_replies(status);
CREATE INDEX IF NOT EXISTS idx_zulip_chat_replies_created ON zulip_chat_replies(created_at);
CREATE TABLE IF NOT EXISTS zulip_chat_topics (
  channel TEXT NOT NULL,
  topic TEXT NOT NULL,
  agent_label TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (channel, topic)
);
CREATE INDEX IF NOT EXISTS idx_zulip_chat_topics_agent ON zulip_chat_topics(agent_label);
"""

# Slack live-chat claim/continuity state (task #559 Gate 3). Separate from
# Zulip tables so dual-write is impossible by construction.
_SLACK_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_chat_replies (
  inbound_event_id TEXT PRIMARY KEY,
  agent_label TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reply_ts TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slack_chat_replies_status ON slack_chat_replies(status);
CREATE INDEX IF NOT EXISTS idx_slack_chat_replies_updated ON slack_chat_replies(updated_at);
CREATE TABLE IF NOT EXISTS slack_chat_conversations (
  conversation_id TEXT PRIMARY KEY,
  agent_label TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slack_chat_conversations_agent ON slack_chat_conversations(agent_label);
-- Outbound Slack send slots (task #1640): the dedupe key and hourly-cap
-- reservation for slack_outbound.send_message. Deliberately NOT event_log: a
-- reservation must be releasable when the POST fails, and event_log rows are
-- hash-chained, so deleting one orphans the next row's prev_hash. Only a
-- completed send is appended to the ledger.
CREATE TABLE IF NOT EXISTS slack_outbound_slots (
  dedupe_key TEXT PRIMARY KEY,
  reserved_at TEXT NOT NULL,
  event_ulid TEXT
);
CREATE INDEX IF NOT EXISTS idx_slack_outbound_slots_reserved ON slack_outbound_slots(reserved_at);
"""

# Structured executor evidence.  This is additive telemetry only: policy and
# lifecycle decisions remain in executor/daemon.py and the existing stores.
#
# task_id/delegation_id are ON DELETE SET NULL, not CASCADE — same rationale
# as agent_sessions.task_id above: task_delete (or a delegation's own cascade)
# must not retroactively erase completed execution evidence. A trust-ladder
# count that changes because the row it was about got deleted later is worse
# than a dangling NULL reference (task #467 / PR #201 review).
_EXECUTOR_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS executor_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  task_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (task_type IN ('code','docs','research','scout','review','operations','unknown')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  outcome TEXT NOT NULL DEFAULT 'unknown'
    CHECK (outcome IN ('success','failure','blocked','quarantined','cooldown','killed','waiting_review','unknown')),
  detail TEXT,
  failure_class TEXT
);
CREATE INDEX IF NOT EXISTS idx_executor_attempts_task ON executor_attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_executor_attempts_delegation ON executor_attempts(delegation_id);
CREATE INDEX IF NOT EXISTS idx_executor_attempts_outcome ON executor_attempts(outcome);
"""

# --- end event_log --------------------------------------------------------------


# --- agent registry + sessions (web control center M1) --------------------------
# Until now an "agent" was a bare label string denormalized across agent_events,
# tasks.claimed_by, memory.author and friends, and liveness could only be inferred
# from check_anomalies' silent_agent rule at 2-day granularity. These two tables
# make "which agents and sessions are alive right now" a fact rather than a guess.
#
# Deliberately NO status column on `agents`. Status is derived at read time by
# registry.derive_status() from last_seen. A stored status lies exactly when the
# process that should have updated it died — which is the moment you care.
#
# agent_sessions.task_id is ON DELETE SET NULL, not CASCADE: connect() enables
# foreign_keys, and deleting a task must not erase the record that an agent spent
# 40 minutes running for it.
#
# control_flags and ui_sessions are created here, unused by M1's read-only surface,
# so that M2 (executor pause, browser auth) needs no second migration against a live
# memory.db that is synced across machines. Adding semantics to an existing table is
# free; adding the table later is not — the same reasoning as event_log's
# episode_id above.
_AGENT_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  label TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'unknown',
  host TEXT,
  known INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  last_seen_via TEXT,
  pid INTEGER,
  version TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents(last_seen);
CREATE INDEX IF NOT EXISTS idx_agents_known ON agents(known);

CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK(status IN ('starting','running','waiting','completed','failed','abandoned')),
  started_at TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL,
  ended_at TEXT,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  delegation_id INTEGER,
  worktree_path TEXT,
  branch TEXT,
  external_session_id TEXT,
  transcript_path TEXT,
  host TEXT,
  pid INTEGER,
  episode_id TEXT,
  exit_status TEXT,
  summary TEXT,
  model TEXT,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent ON agent_sessions(agent, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_task ON agent_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_ext ON agent_sessions(external_session_id);

CREATE TABLE IF NOT EXISTS control_flags (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  reason TEXT,
  set_by TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ui_sessions (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen TEXT,
  user_agent TEXT,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ui_sessions_expires ON ui_sessions(expires_at);

-- A Control Center session established through WorkOS AuthKit (task #2114).
-- session_id is ui_sessions.id (sha256 of the cookie). sealed is the WorkOS
-- sealed session, Fernet-encrypted with WORKOS_COOKIE_PASSWORD, so a reader of
-- this database without that key holds no usable credential. It is kept
-- server-side so the access token can be refreshed and membership re-checked
-- without the browser ever holding it.
-- A signed-out session retains this authentication-kind marker with sealed=''
-- and access_expires_at=0: an in-flight reader of ui_sessions must not fall
-- back to bearer-style authentication after the credential is erased.
CREATE TABLE IF NOT EXISTS ui_session_workos (
  session_id TEXT PRIMARY KEY NOT NULL,
  sealed TEXT NOT NULL,
  workos_user_id TEXT NOT NULL,
  workos_organization_id TEXT NOT NULL,
  workos_session_id TEXT NOT NULL,
  access_expires_at INTEGER NOT NULL,
  checked_at REAL NOT NULL,
  created_at TEXT NOT NULL
);

-- monitor._launchd_zero_runs_status's durable anchor for "how long has this
-- LaunchAgent actually been loaded with zero runs". launchd exposes no
-- bootstrap timestamp, and the plist's own mtime is unreliable in both
-- directions for this repo's deploy scripts: it can predate the actual
-- `launchctl bootstrap` (install-then-enable-later flow), or postdate it when
-- an unrelated redeploy rewrites the file without a bootout+bootstrap cycle
-- (a long-broken timer would then read as freshly installed). Recording the
-- first time THIS process observed a label loaded and never-run gives an
-- anchor that no later, unrelated file rewrite can silently reset — set once
-- per label, never overwritten.
CREATE TABLE IF NOT EXISTS launchd_first_seen (
  label TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL
);
"""

# Index names asserted by _migrate() and by tests/test_storage_migrations.py. Kept
# as a module constant so the migration and the test agree by construction rather
# than by two hand-maintained lists drifting apart.
_AGENT_REGISTRY_INDEXES = (
    ("idx_agents_last_seen", "agents(last_seen)"),
    ("idx_agents_known", "agents(known)"),
    ("idx_agent_sessions_agent", "agent_sessions(agent, started_at)"),
    ("idx_agent_sessions_status", "agent_sessions(status)"),
    ("idx_agent_sessions_task", "agent_sessions(task_id)"),
    ("idx_agent_sessions_ext", "agent_sessions(external_session_id)"),
    ("idx_ui_sessions_expires", "ui_sessions(expires_at)"),
)

# --- end agent registry ---------------------------------------------------------


# --- host reports (task #798 Devices pane) ------------------------------------
# system_doctor reports per-unit status on the process's own host. This table
# stores that snapshot (and optional remote heartbeats) so the hub can aggregate
# fleet hosts without inventing live unit state for machines that never reported.
_HOST_REPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_reports (
  host TEXT PRIMARY KEY,
  last_seen TEXT NOT NULL,
  last_seen_via TEXT,
  version TEXT,
  doctor_as_of TEXT,
  doctor_json TEXT,
  executor_json TEXT,
  worktrees_json TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_reports_last_seen ON host_reports(last_seen);
"""


# --- outbound connector OAuth (J5) --------------------------------------------
_CONNECTOR_OAUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_oauth_pending (
  state_hash TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  org_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  device_node TEXT,
  code_verifier TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_oauth_connections (
  provider TEXT NOT NULL,
  org_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('connected', 'needs_reconnect', 'disconnected')),
  scopes_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (provider, org_id)
);
CREATE INDEX IF NOT EXISTS idx_connector_oauth_pending_expiry
  ON connector_oauth_pending(expires_at);
CREATE INDEX IF NOT EXISTS idx_connector_oauth_pending_org_id
  ON connector_oauth_pending(org_id);
CREATE INDEX IF NOT EXISTS idx_connector_oauth_connections_org_id
  ON connector_oauth_connections(org_id, provider);
"""


# --- ua-node (task #1327) -------------------------------------------------------
# `unified-agent node` connects OUT to the hub, registers host facts, and pulls
# work from a per-node queue. `nodes` binds a node name to the agent identity that
# first registered it (a second identity cannot claim another device's work);
# `node_dispatch` is the per-node queue. A receipt is written at most once per
# item — `receipt_json` non-NULL is the idempotency gate.
_NODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  node TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  device_node TEXT,
  facts_json TEXT,
  version TEXT,
  registered_at TEXT NOT NULL,
  last_heartbeat TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_dispatch (
  id TEXT PRIMARY KEY,
  node TEXT NOT NULL,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  prompt TEXT NOT NULL,
  cwd TEXT NOT NULL,
  model TEXT,
  timeout_s INTEGER NOT NULL,
  run_id TEXT,
  task_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  lease_expires_at TEXT,
  completed_at TEXT,
  receipt_json TEXT,
  session_id TEXT,
  hermes_profile TEXT,
  profile_agent TEXT,
  permission_mode TEXT,
  settings_source TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_dispatch_node_status
  ON node_dispatch(node, status, created_at);
"""


# A proof authorizes one HTTP attempt. Keep replay fences durable across workers
# and restarts; the verifier removes only expired entries with bounded work.
_DEVICE_PROOF_REPLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_proof_replays (
  device_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (device_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_device_proof_replays_expires
  ON device_proof_replays(expires_at);
"""


# --- harness config bundles (task #1330, I4) -----------------------------------
# Hub-published versioned skills/rules/MCP bundle. ua-node renders per-harness
# files in proposal mode; apply requires a one-shot harness_config_apply approval.
_HARNESS_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_config_bundles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL UNIQUE,
  published_at TEXT NOT NULL,
  published_by TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0,
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_harness_config_bundles_active
  ON harness_config_bundles(active);
CREATE INDEX IF NOT EXISTS idx_harness_config_bundles_org_id
  ON harness_config_bundles(org_id);
CREATE TABLE IF NOT EXISTS harness_config_proposals (
  id TEXT PRIMARY KEY,
  node TEXT NOT NULL,
  surface TEXT NOT NULL,
  path TEXT NOT NULL,
  bundle_version TEXT NOT NULL,
  desired_hash TEXT NOT NULL,
  observed_hash TEXT,
  diff_text TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  approval_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_harness_config_proposals_node
  ON harness_config_proposals(node, status, created_at);
CREATE INDEX IF NOT EXISTS idx_harness_config_proposals_org_id
  ON harness_config_proposals(org_id);
CREATE TABLE IF NOT EXISTS harness_config_drift (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node TEXT NOT NULL,
  surface TEXT NOT NULL,
  path TEXT NOT NULL,
  bundle_version TEXT NOT NULL,
  observed_hash TEXT,
  desired_hash TEXT NOT NULL,
  reported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_config_drift_node
  ON harness_config_drift(node, reported_at);
"""

# --- self-organization proposals (task #1431) --------------------------------
# Proposing is always on by default; applying is strictly human/policy gated.
_ORG_PROPOSALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_proposals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  proposal_type TEXT NOT NULL,
  author TEXT NOT NULL,
  scope TEXT NOT NULL,
  rationale TEXT NOT NULL,
  proposed_changes TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'reviewed', 'applied', 'rejected')),
  approval_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_at TEXT,
  reviewed_by TEXT,
  review_notes TEXT,
  applied_at TEXT,
  applied_by TEXT,
  rejected_at TEXT,
  rejected_by TEXT,
  resolution_reason TEXT,
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_proposals_scope
  ON org_proposals(scope, status);
CREATE INDEX IF NOT EXISTS idx_org_proposals_author
  ON org_proposals(author);
CREATE INDEX IF NOT EXISTS idx_org_proposals_status_created
  ON org_proposals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_org_proposals_org_id ON org_proposals(org_id);
"""


# --- idempotency (task 429) ---------------------------------------------------
# Fast path for retry-safety across tool timeouts and retried writes: a retried
# call with the same idempotency key returns the committed result without creating
# a duplicate row.
_IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created ON idempotency_keys(created_at);
"""


def _apply_idempotency_schema(conn: sqlite3.Connection) -> None:
    """Apply ``_IDEMPOTENCY_SCHEMA`` without ``executescript``'s implicit COMMIT.

    ``_migrate`` runs inside ``init_db``'s transaction. ``executescript`` would
    commit that transaction before the CREATE. Statement-at-a-time ``execute``
    keeps the legacy migration's commit point.
    """
    for statement in _IDEMPOTENCY_SCHEMA.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def idempotency_storage_key(key: str) -> str:
    """The stored form of a caller's idempotency key, scoped to its tenant (#1796).

    Keys are one global PRIMARY KEY, so without a scope org_b replaying org_a's
    key got org_a's receipt back (and a used key blocked the other tenant).
    Under a tenant the stored key carries the org, so each tenant has its own
    key space: another tenant's key is simply absent. The untenanted private
    hub (tenant None) stores the key unchanged, so existing receipts still
    replay. Every read or write of a caller-supplied key goes through here.

    A caller key may not contain the separator: otherwise a raw key spelled
    ``org:<tenant>\x1f<key>`` would be the same PRIMARY KEY as that tenant's
    scoped receipt, and could replay or replace it.
    """
    if _TENANT_KEY_SEP in key:
        raise ValueError("idempotency key must not contain the U+001F unit separator")
    tenant = edition.tenant_org_id()
    return key if tenant is None else f"org:{tenant}{_TENANT_KEY_SEP}{key}"


_TENANT_KEY_SEP = "\x1f"


class AmbiguousIdempotencyReceipt(ValueError):
    """A pre-scoping receipt exists for this key, but its tenant is unknown."""


def _refuse_unscoped_receipt(conn: sqlite3.Connection, key: str) -> None:
    """Stop a retry that may already have run under an unscoped receipt (#1834).

    Receipts written before tenant scoping use the raw key and carry no
    tenant, so a scoped lookup misses them and the write would repeat.
    Replaying one could hand another tenant's result back, so neither replay
    nor re-execution is safe: refuse and ask for a fresh key.
    """
    if edition.tenant_org_id() is None:
        return
    row = conn.execute("SELECT tool FROM idempotency_keys WHERE key=?", (key,)).fetchone()
    if row is not None:
        raise AmbiguousIdempotencyReceipt(
            f"idempotency key {key!r} has a receipt written before tenant scoping; "
            "its tenant cannot be established, so the request was not repeated. "
            "Retry with a new idempotency key."
        )


def read_idempotency_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """The ``(tool, result_json)`` row for a caller key, under its tenant scope.

    For a writer that keeps its own receipt states and needs the row whatever
    its tool (task #1938). It applies the same tenant scoping and the same
    pre-scoping refusal as ``get_idempotency_record``: a raw receipt that a
    scoped lookup misses raises ``AmbiguousIdempotencyReceipt`` instead of
    reading as absent, so the write is not repeated.
    """
    row = conn.execute(
        "SELECT tool, result_json FROM idempotency_keys WHERE key=?",
        (idempotency_storage_key(key),),
    ).fetchone()
    if row is None:
        _refuse_unscoped_receipt(conn, key)
    return row


def get_idempotency_record(conn: sqlite3.Connection, key: str, tool: str) -> dict | None:
    """Retrieve the cached result of an idempotent tool execution if present."""
    row = conn.execute(
        "SELECT result_json FROM idempotency_keys WHERE key=? AND tool=?",
        (idempotency_storage_key(key), tool),
    ).fetchone()
    if row is None:
        _refuse_unscoped_receipt(conn, key)
        return None
    try:
        return json.loads(row["result_json"])
    except Exception:
        return None


def try_claim_idempotency(conn: sqlite3.Connection, key: str, tool: str) -> bool:
    """Atomically claim an idempotency key with a pending placeholder.

    ``key`` is the PRIMARY KEY alone, so ``INSERT OR IGNORE`` serializes
    concurrent claimants. Returns True when this caller won the claim
    (``rowcount == 1``); False when the key was already taken.
    """
    stored = idempotency_storage_key(key)
    if conn.execute("SELECT 1 FROM idempotency_keys WHERE key=?", (stored,)).fetchone() is None:
        _refuse_unscoped_receipt(conn, key)
    cur = conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys (key, tool, result_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (stored, tool, json.dumps({"pending": True}), now()),
    )
    return cur.rowcount == 1


def record_idempotency(conn: sqlite3.Connection, key: str, tool: str, result: dict) -> None:
    """Cache the result of an idempotent tool execution. Never raises."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO idempotency_keys (key, tool, result_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (idempotency_storage_key(key), tool, json.dumps(result), now()),
        )
    except Exception:
        pass


def record_idempotency_required(
    conn: sqlite3.Connection, key: str, tool: str, result: dict
) -> None:
    """Write a replay receipt on a caller-owned transaction, propagating errors."""
    stored = idempotency_storage_key(key)
    existing = conn.execute("SELECT tool FROM idempotency_keys WHERE key=?", (stored,)).fetchone()
    if existing is not None and existing["tool"] != tool:
        raise ValueError(f"idempotency key {key!r} is already registered for {existing['tool']!r}")
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, tool, result_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (stored, tool, json.dumps(result), now()),
    )


# --- request receipts (tasks #2480, #2515) --------------------------------------
# A browser write keyed by an ``Idempotency-Key`` header: the key is reserved for
# one request (bound by a digest of its body), the write runs once, and its first
# successful answer is frozen so every repeat gets it back. The rows live in
# ``idempotency_keys`` under ``idempotency_storage_key``, so they are tenant
# scoped; callers put their own identity into ``logical_key`` to scope them per
# caller. Routine "Run now" and the vault append and journal routes share this.


class ReceiptConflict(ValueError):
    """The key was already used for a different request (another digest)."""


class ReceiptInFlight(ValueError):
    """The same request is still being processed under this key."""


class ReceiptOutcomeUnknown(ValueError):
    """An earlier attempt under this key started and never settled (task #2515).

    Raised instead of a takeover when the caller passed ``takeover=False``: the
    write may or may not have happened, so it is not run again automatically.
    """


def request_receipt_begin(
    tool: str,
    logical_key: str,
    digest: str,
    *,
    ttl: timedelta,
    stale: timedelta,
    prune_batch: int = 500,
    takeover: bool = True,
) -> tuple[str | None, dict | None]:
    """Reserve ``logical_key`` for this request or return its stored first answer.

    Returns ``(owner, None)`` when the caller now owns the reservation and must
    run the write, then call ``request_receipt_finish`` with ``owner``;
    ``(None, response)`` when the key already settled for the same request.
    Raises ``ReceiptConflict`` when the key was used for a different digest,
    ``ReceiptInFlight`` while another request with it is still running (a
    reservation older than ``stale`` is taken over), and
    ``AmbiguousIdempotencyReceipt`` for a receipt written before tenant scoping.
    Prunes up to ``prune_batch`` of ``tool``'s receipts older than ``ttl``.

    ``takeover=False`` is for a write that is not itself idempotent (a vault
    append, task #2515): a reservation older than ``stale``, or one marked by
    ``request_receipt_abandon``, raises ``ReceiptOutcomeUnknown`` instead of
    being taken over, and only settled receipts are pruned, so an attempt
    whose outcome is unknown is never run again under the same key. Takeover
    is safe only when the write underneath dedupes by a key of its own, as
    ``routine_fire`` does for Run now.
    """
    import uuid

    stored_key = idempotency_storage_key(logical_key)
    owner = uuid.uuid4().hex
    now_dt = datetime.now(UTC)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            settled_only = "" if takeover else " AND instr(result_json, '\"response\"') > 0"
            conn.execute(
                "DELETE FROM idempotency_keys WHERE rowid IN (SELECT rowid FROM "
                f"idempotency_keys WHERE tool=? AND created_at < ?{settled_only} LIMIT ?)",
                (tool, (now_dt - ttl).isoformat(), prune_batch),
            )
            row = read_idempotency_row(conn, logical_key)
            pending = json.dumps({"digest": digest, "owner": owner})
            if row is None:
                conn.execute(
                    "INSERT INTO idempotency_keys (key, tool, result_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (stored_key, tool, pending, now_dt.isoformat()),
                )
                conn.commit()
                return owner, None
            record = json.loads(row["result_json"]) if row["tool"] == tool else {}
            if record.get("digest") != digest:
                raise ReceiptConflict("the key was already used for a different request")
            if record.get("response") is not None:
                conn.commit()
                return None, record["response"]
            if record.get("outcome") == "unknown":
                raise ReceiptOutcomeUnknown("an earlier attempt with this key did not settle")
            created = conn.execute(
                "SELECT created_at FROM idempotency_keys WHERE key=?", (stored_key,)
            ).fetchone()["created_at"]
            if created > (now_dt - stale).isoformat():
                raise ReceiptInFlight("the request with this key is still in flight")
            if not takeover:
                raise ReceiptOutcomeUnknown("an earlier attempt with this key did not settle")
            conn.execute(
                "UPDATE idempotency_keys SET result_json=?, created_at=? WHERE key=?",
                (pending, now_dt.isoformat(), stored_key),
            )
            conn.commit()
            return owner, None
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def request_receipt_finish(
    tool: str, logical_key: str, digest: str, owner: str, response: dict | None
) -> tuple[bool, dict | None]:
    """Settle the reservation ``owner`` holds: ``(still_owner, canonical answer)``.

    With a ``response`` the reservation becomes the immutable first answer; with
    None (nothing ran: refused, parked, failed) it is dropped so the request can
    be repeated. Only the owner settles or drops it. A caller that lost
    ownership (its reservation was taken over) gets ``(False, stored answer or
    None)`` and must return that, never its own local answer.
    """
    stored_key = idempotency_storage_key(logical_key)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (stored_key,)
            ).fetchone()
            record = (
                json.loads(row["result_json"]) if row is not None and row["tool"] == tool else {}
            )
            if record.get("owner") != owner or record.get("digest") != digest:
                conn.commit()
                return False, record.get("response")
            if response is None:
                conn.execute("DELETE FROM idempotency_keys WHERE key=?", (stored_key,))
            else:
                conn.execute(
                    "UPDATE idempotency_keys SET result_json=? WHERE key=?",
                    (json.dumps({"digest": digest, "response": response}), stored_key),
                )
            conn.commit()
            return True, response
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def request_receipt_abandon(tool: str, logical_key: str, digest: str, owner: str) -> bool:
    """Mark the reservation ``owner`` holds as outcome unknown (task #2515).

    For an attempt that ended without an answer (an exception or a cancelled
    request after the write may have started). The key then answers
    ``ReceiptOutcomeUnknown`` on every repeat instead of running again. Returns
    False when ``owner`` no longer holds the reservation.
    """
    stored_key = idempotency_storage_key(logical_key)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key=?", (stored_key,)
        ).fetchone()
        record = json.loads(row["result_json"]) if row is not None and row["tool"] == tool else {}
        if record.get("owner") != owner or record.get("digest") != digest:
            conn.commit()
            return False
        conn.execute(
            "UPDATE idempotency_keys SET result_json=? WHERE key=?",
            (json.dumps({"digest": digest, "outcome": "unknown"}), stored_key),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- agent_perf_samples (task 64 phase B) ---------------------------------------
# Companion table to agent_events: one row per instrumented tool call, capturing
# response latency, success/error, and a process memory (RSS) sample. Written
# only from monitor.instrumented() (the MCP registration wrapper in server.py),
# so direct/unwrapped calls in unit tests never populate it. Kept in its own
# executescript block for the same rebase-hygiene reason as agent_events.
_AGENT_PERF_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_perf_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  tool TEXT NOT NULL,
  latency_ms REAL NOT NULL,
  ok INTEGER NOT NULL,
  error TEXT,
  rss_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_perf_ts ON agent_perf_samples(ts);
"""
# No index on `tool`: every reader (monitor.perf_summary, scripts/gate_replay,
# the webui perf endpoints) filters `ts >= <=24h cutoff` then groups by tool, so
# idx_agent_perf_ts alone serves them. A standalone tool index just doubled the
# table's on-disk cost (task #919); _migrate() drops it from pre-existing DBs.


# Task #925: agent_perf_samples write-rate reduction (sampling and exclusions)
_clock = time.monotonic
_perf_sample_lock = threading.Lock()
_tool_call_history: dict[str, list[float]] = {}
_tool_sample_counters: dict[str, int] = {}


def _reset_perf_sample_state() -> None:
    """Reset in-memory rate tracking and sampling counters (for tests/restarts)."""
    with _perf_sample_lock:
        _tool_call_history.clear()
        _tool_sample_counters.clear()


def perf_sample_exclude_tools() -> set[str]:
    """Colon-separated tool names whose OK perf samples are dropped.

    Configured via ``UA_PERF_SAMPLE_EXCLUDE_TOOLS``. Errors are never dropped.
    """
    raw = os.environ.get("UA_PERF_SAMPLE_EXCLUDE_TOOLS")
    if not raw:
        return set()
    return {item.strip() for item in raw.split(":") if item.strip()}


def perf_sample_rate_threshold() -> float | None:
    """Call-rate threshold (calls/sec) above which OK perf samples are 1-in-N sampled.

    Configured via ``UA_PERF_SAMPLE_RATE_THRESHOLD`` (or legacy ``UA_PERF_SAMPLE_RATE``).
    None or <= 0 disables rate-based sampling.
    """
    raw = os.environ.get("UA_PERF_SAMPLE_RATE_THRESHOLD") or os.environ.get("UA_PERF_SAMPLE_RATE")
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def perf_sample_interval() -> int:
    """Sampling interval N for 1-in-N sampling (default 10).

    Configured via ``UA_PERF_SAMPLE_INTERVAL`` (or ``UA_PERF_SAMPLE_N``).
    """
    raw = os.environ.get("UA_PERF_SAMPLE_INTERVAL") or os.environ.get("UA_PERF_SAMPLE_N")
    if not raw:
        return 10
    try:
        val = int(raw)
        return val if val > 1 else 10
    except ValueError:
        return 10


def should_record_perf_sample(tool: str, ok: bool = True) -> bool:
    """Deterministic predicate determining whether a perf sample should be written.

    Rules (task #925):
    1. Errors are NEVER dropped under any policy (ok=False always returns True).
       Any configured policy that would drop an error sample is refused/ignored.
    2. Excluded tools (UA_PERF_SAMPLE_EXCLUDE_TOOLS) drop OK samples unconditionally.
    3. Above rate threshold (UA_PERF_SAMPLE_RATE_THRESHOLD), OK samples are 1-in-N sampled.
    4. With no env vars set, returns True for every call (byte-for-byte prior default).
    """
    if not ok:
        return True

    try:
        excluded = perf_sample_exclude_tools()
        if tool in excluded:
            return False

        rate_thresh = perf_sample_rate_threshold()
        if rate_thresh is not None:
            now_mono = _clock()
            interval = perf_sample_interval()
            with _perf_sample_lock:
                timestamps = _tool_call_history.setdefault(tool, [])
                cutoff = now_mono - 1.0
                timestamps = [t for t in timestamps if t >= cutoff]
                timestamps.append(now_mono)
                _tool_call_history[tool] = timestamps
                if len(timestamps) > rate_thresh:
                    count = _tool_sample_counters.get(tool, 0) + 1
                    _tool_sample_counters[tool] = count
                    if count % interval != 0:
                        return False
                else:
                    _tool_sample_counters[tool] = 0
        return True
    except Exception:
        # Best-effort safety: any failure in sampling predicate records the sample
        return True


def record_perf_sample(
    tool: str,
    latency_ms: float,
    ok: bool = True,
    error: str | None = None,
    rss_bytes: int | None = None,
) -> None:
    """Best-effort append of one row to agent_perf_samples. Never raises.

    A perf sample is bookkeeping layered on a tool call (task 64 phase B) and
    must never break it — any failure here is swallowed. `error` is truncated
    to 200 chars. Hot poll tools may be sampled or excluded via
    UA_PERF_SAMPLE_EXCLUDE_TOOLS or UA_PERF_SAMPLE_RATE_THRESHOLD (task #925).
    Error samples are never dropped.
    """
    try:
        if not should_record_perf_sample(tool, ok=ok):
            return
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO agent_perf_samples (ts, tool, latency_ms, ok, error, rss_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now(),
                    tool,
                    latency_ms,
                    1 if ok else 0,
                    error[:200] if error else error,
                    rss_bytes,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def prune_perf_samples(older_than_days: float | None = None) -> int:
    """Delete agent_perf_samples rows older than the retention window.

    Returns the number of rows removed. Unlike the append-only event log, perf
    samples are disposable telemetry whose only consumer is a rolling window of
    at most 24h, so this is safe to run automatically — backup_runner calls it
    on the daily backup cadence. ``0`` days retention keeps everything.
    """
    from datetime import UTC, datetime, timedelta

    days = (
        config.perf_sample_retention_days() if older_than_days is None else float(older_than_days)
    )
    if days <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM agent_perf_samples WHERE ts < ?", (cutoff,))
        conn.commit()
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        # Hand freed pages back to the OS when the DB was built with
        # auto_vacuum=INCREMENTAL (a one-time migration on the hub). A no-op
        # under auto_vacuum=NONE, so it is always safe to call.
        try:
            conn.execute("PRAGMA incremental_vacuum")
            conn.commit()
        except Exception:
            pass
        return removed
    finally:
        conn.close()


# --- end agent_perf_samples -------------------------------------------------------


# --- anomaly_notifications (task 64 phase C) --------------------------------------
# Dedupe/cooldown ledger for pushed anomaly alerts: one row per (rule, subject)
# pair that has been notified, so monitor.check_anomalies_and_notify doesn't
# re-page the same anomaly on every poll. Managed entirely within monitor.py
# (it is the only reader/writer); the schema lives here alongside the other
# additive companion tables.
_ANOMALY_NOTIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS anomaly_notifications (
  rule TEXT NOT NULL,
  subject TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_notified_at TEXT NOT NULL,
  notify_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (rule, subject)
);
"""


def _migrate_anomaly_notifications(conn: sqlite3.Connection) -> None:
    """Add `notify_count` (escalating re-notify backoff) to a pre-existing ledger.

    Idempotent and additive; not a SCHEMA_VERSION bump. Rows that predate the
    column count as one notification, so their next gap is the base cooldown.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(anomaly_notifications)")}
    if "notify_count" not in cols:
        conn.execute(
            "ALTER TABLE anomaly_notifications ADD COLUMN notify_count INTEGER NOT NULL DEFAULT 1"
        )


# --- end anomaly_notifications -----------------------------------------------------


# --- db_size_samples (task 64 phase D) ---------------------------------------------
# History of memory.db on-disk size, recorded by monitor.check_anomalies_and_notify
# (the */30 cron path) and read by the db_size_growth anomaly rule. Same
# additive-block pattern as the tables above.
_DB_SIZE_SCHEMA = """
CREATE TABLE IF NOT EXISTS db_size_samples (
  ts TEXT NOT NULL,
  bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_db_size_ts ON db_size_samples(ts);
"""

# --- end db_size_samples -----------------------------------------------------------


# --- delegation + approvals (task 62) -----------------------------------------
# Additive tables for the delegation protocol and the opt-in approval queue.
# `delegations` links a delegated child task to its from/to agents and records
# which callback fires when the child completes. `approvals` is the global
# approval queue: a gated tool call parks here as 'pending' until a human (or
# the steward, on the operator's direction) resolves it; an 'approved' row is a
# one-shot pass consumed by the retried call. Kept in its own executescript
# block — separate from _SCHEMA — so sibling branches editing _SCHEMA rebase
# trivially around it (same pattern as _AGENT_EVENTS_SCHEMA above).
_DELEGATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  task_spec TEXT NOT NULL,
  deadline TEXT,
  callback TEXT NOT NULL DEFAULT 'notify'
    CHECK (callback IN ('notify','memory','none')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','in_flight','completed','cancelled','failed')),
  memory_id TEXT,
  callback_result TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  claim_generation INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  -- Nullable tenant boundary (task #1232); see the memory table's org_id
  -- comment in _SCHEMA above for the gating rule.
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_delegations_task ON delegations(task_id);
CREATE INDEX IF NOT EXISTS idx_delegations_to_agent ON delegations(to_agent);
CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations(status);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  requested_by TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','approved','denied','consumed','revoked','expired')),
  reason TEXT,
  resolved_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  -- Nullable tenant boundary (task #1232); see the memory table's org_id
  -- comment in _SCHEMA above for the gating rule.
  org_id TEXT,
  -- Display linkage to the caller's current run (task #1653).
  -- Nullable and stamped only at request time. Never an authorization input.
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_tool ON approvals(tool);
-- idx_approvals_run_id is created in _add_approval_run_id(), not here: on a
-- pre-existing DB this script runs before that column is ALTERed in, and
-- CREATE INDEX on it would fail instead of being a no-op.
"""
# --- end delegation + approvals ------------------------------------------------


# --- elicitation challenges (task #832) ---------------------------------------
# Server-issued one-time nonce bound to an approval row. The plaintext is
# returned once in InputRequiredResult.request_state; only the SHA-256 hash
# is stored. consumed_at is set atomically on a resolving accept — decline
# and cancel leave it NULL. A separate table (not extra approvals columns)
# so sibling branches editing the approvals CHECK rebuild rebase around it.
_ELICITATION_CHALLENGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS elicitation_challenges (
  approval_id INTEGER PRIMARY KEY REFERENCES approvals(id) ON DELETE CASCADE,
  nonce_hash TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  consumed_at TEXT,
  session_key TEXT
);
"""
# --- end elicitation challenges -----------------------------------------------


# --- session friction nudges (task #914) ------------------------------------
# Dedupe-once ledger for the session-end friction nudge (TeamAI emulation F1).
# One row per session that has ALREADY been nudged toward a learning capture, so
# a re-evaluation of the same closed session never emits a second nudge. Managed
# entirely within session_friction.py (its only reader/writer); additive block
# for the same rebase-hygiene reason as the companion tables above.
_FRICTION_NUDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_friction_nudges (
  session_id TEXT PRIMARY KEY,
  score INTEGER NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]',
  nudged_at TEXT NOT NULL
);
"""
# --- end session friction nudges --------------------------------------------


# --- recall self-curation (task #915) --------------------------------------
# TeamAI emulation F2. memory_recall_votes: dual-counter per note mirroring
# teamai-cli votes.ts — recalled_count auto-increments every time memory_search
# returns the note, upvoted_count on an explicit "helpful" feedback. Both feed a
# vote term in memory_search ranking. recall_quality_log: one row per search
# (result count + top score) so a maintenance pass can spot knowledge gaps.
# Managed entirely within recall_curation.py; additive block, rebase-hygiene as
# the companion tables above.
_RECALL_CURATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_recall_votes (
  note_id TEXT PRIMARY KEY NOT NULL,
  recalled_count INTEGER NOT NULL DEFAULT 0,
  upvoted_count INTEGER NOT NULL DEFAULT 0,
  last_recalled_at TEXT
);
CREATE TABLE IF NOT EXISTS recall_quality_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  query TEXT NOT NULL,
  result_count INTEGER NOT NULL,
  top_score REAL,
  hit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recall_quality_ts ON recall_quality_log(ts);
"""
# --- end recall self-curation ---------------------------------------------


# --- memory index refresh queue (task #1914) ---------------------------------
# Tags whose auto-maintained index notes (masterindex/tagindex_*) need a
# rebuild. A memory write marks its tags here inside its own transaction and
# the rebuild runs after that transaction commits, so the scan over the
# tenant's notes never extends the note's write lock. A rebuild that fails
# (say, busy past the timeout) leaves its rows for the next write or index
# read to retry. org_key is the tenant org id, '' for the untenanted hub, so
# the primary key also dedupes the NULL tenant. Managed in tools/memory.py.
_MEMORY_INDEX_DIRTY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_index_dirty (
  org_key TEXT NOT NULL,
  tag TEXT NOT NULL,
  generation TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (org_key, tag)
);
"""
# --- end memory index refresh queue ------------------------------------------


# --- execution graph edges (task #256) ----------------------------------------
_EXECUTION_GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, depends_on_task_id),
  CHECK(task_id != depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_task ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_prerequisite
  ON task_dependencies(depends_on_task_id);
"""
# --- end execution graph edges -------------------------------------------------


# --- task subtree holds (task #1747) -------------------------------------------
# One active hold per root task (the partial unique index; COALESCE because two
# NULL org_ids would not collide). A released hold stays as history, and a later
# pause creates a new record. Members are the subtree snapshotted at hold time,
# each with its status then, so a restore can tell what the hold itself changed:
# which run it cancelled (cancelled_run_id/agent) and when a member was restored.
# cancelling_at is set while a cancel is still recording its requests. No
# foreign key to tasks: a hold record outlives a deleted task. See task_holds.py;
# _migrate_task_holds adds the later columns to an older table.
_TASK_HOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_holds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_task_id INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('pause','cancel')),
  status TEXT NOT NULL CHECK(status IN ('active','released')),
  reason TEXT NOT NULL DEFAULT '',
  release_policy TEXT NOT NULL DEFAULT 'manual'
    CHECK(release_policy IN ('manual','after_active_runs_finish')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  released_by TEXT,
  released_at TEXT,
  org_id TEXT,
  cancelling_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_holds_active_root
  ON task_holds(root_task_id, COALESCE(org_id, '')) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_task_holds_status ON task_holds(status);
CREATE INDEX IF NOT EXISTS idx_task_holds_org_id ON task_holds(org_id);
CREATE TABLE IF NOT EXISTS task_hold_members (
  hold_id INTEGER NOT NULL REFERENCES task_holds(id),
  task_id INTEGER NOT NULL,
  status_at_hold TEXT NOT NULL,
  cancel_requested_by_hold INTEGER NOT NULL DEFAULT 0,
  cancelled_run_id TEXT,
  cancelled_run_agent TEXT,
  restored_at TEXT,
  PRIMARY KEY (hold_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_hold_members_task ON task_hold_members(task_id);
"""


def _migrate_task_holds(conn: sqlite3.Connection) -> None:
    """Additive columns for a task-hold table created before they existed."""
    hold_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_holds)")}
    if "cancelling_at" not in hold_cols:
        conn.execute("ALTER TABLE task_holds ADD COLUMN cancelling_at TEXT")
    member_cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_hold_members)")}
    for col in ("cancelled_run_id", "cancelled_run_agent", "restored_at"):
        if col not in member_cols:
            conn.execute(f"ALTER TABLE task_hold_members ADD COLUMN {col} TEXT")


# --- end task subtree holds ------------------------------------------------------


# --- bounded autonomous cycles (task #259) -----------------------------------
_AUTONOMOUS_CYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomous_cycles (
  id TEXT PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  plan_id TEXT,
  delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
  session_id TEXT,
  state TEXT NOT NULL DEFAULT 'plan'
    CHECK (state IN ('plan','delegated','executing','validating','reviewing',
                     'rework','pr','blocked','completed','failed','killed')),
  max_rework INTEGER NOT NULL DEFAULT 2 CHECK (max_rework >= 0),
  rework_count INTEGER NOT NULL DEFAULT 0 CHECK (rework_count >= 0),
  max_time_s REAL NOT NULL DEFAULT 3600 CHECK (max_time_s > 0),
  max_tokens INTEGER NOT NULL DEFAULT 100000 CHECK (max_tokens > 0),
  max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries > 0),
  max_cost_cents REAL NOT NULL DEFAULT 1000 CHECK (max_cost_cents > 0),
  used_time_s REAL NOT NULL DEFAULT 0 CHECK (used_time_s >= 0),
  used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
  used_retries INTEGER NOT NULL DEFAULT 0 CHECK (used_retries >= 0),
  used_cost_cents REAL NOT NULL DEFAULT 0 CHECK (used_cost_cents >= 0),
  allowed_capabilities TEXT,
  stop_reason TEXT,
  pr_url TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autonomous_cycles_state ON autonomous_cycles(state);
CREATE INDEX IF NOT EXISTS idx_autonomous_cycles_task ON autonomous_cycles(task_id);

CREATE TABLE IF NOT EXISTS autonomous_cycle_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL REFERENCES autonomous_cycles(id) ON DELETE CASCADE,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(cycle_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_autonomous_cycle_transitions_cycle
  ON autonomous_cycle_transitions(cycle_id, id);
"""
# --- end bounded autonomous cycles -------------------------------------------


# --- approved-dispatch run index (task #548) ----------------------------------
# Index over task + delegation + approval authority — NOT a second mailbox.
# Partial unique index enforces at most one live claim/launch/run per task.
_DISPATCH_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_runs (
  run_id TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
  agent_label TEXT NOT NULL,
  state TEXT NOT NULL
    CHECK (state IN (
      'claimed','launched','running',
      'finished','failed','retryable','blocked',
      'denied','cancelled','expired','refused'
    )),
  actor TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  failure_reason TEXT,
  failure_class TEXT,
  evidence_ref TEXT,
  -- Task #1221: cancellation *recorded* (state='cancelled') is not the same
  -- claim as cessation *confirmed*. NULL/'requested' means termination was
  -- never attempted or its outcome was never durably recorded (e.g. process
  -- crash between the cancel claim and the termination attempt) — treated as
  -- unconfirmed. 'confirmed' means terminate_ours reported already_gone,
  -- terminated, or killed. 'unknown' means it reported refused or
  -- identity_mismatch — the target may still be running.
  termination_outcome TEXT
    CHECK (termination_outcome IS NULL OR termination_outcome IN (
      'requested','confirmed','unknown'
    )),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_runs_task ON dispatch_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_runs_state ON dispatch_runs(state);
CREATE INDEX IF NOT EXISTS idx_dispatch_runs_delegation ON dispatch_runs(delegation_id);
-- Note: idx_dispatch_runs_live_task is managed exclusively by _migrate() below,
-- using DISPATCH_RUN_LIVE_SLOT_PREDICATE.
"""
# --- end approved-dispatch run index -----------------------------------------

# Task #1221: Unified live-slot predicate. Any run with unconfirmed cessation
# (claimed/launched/running, cancelled with unconfirmed outcome, or blocked via
# ceiling timeout) retains its live-slot reservation until cessation is verified.
DISPATCH_RUN_LIVE_SLOT_PREDICATE = (
    "state IN ('claimed','launched','running') "
    "OR (state='cancelled' AND (termination_outcome IS NULL OR termination_outcome != 'confirmed')) "
    "OR (state='blocked' AND failure_reason LIKE 'runtime_ceiling_%' AND "
    "(termination_outcome IS NULL OR termination_outcome != 'confirmed'))"
)


# --- Horizon 2 connector governance (task #664) --------------------------------
# Reviewed MCP/connector registry. Durable revocation lives here; authorization
# still consumes the caller's canonical one-shot approval before use.
_HORIZON2_SCHEMA = """
CREATE TABLE IF NOT EXISTS governed_servers (
  server_id TEXT PRIMARY KEY,
  connector TEXT NOT NULL,
  provenance TEXT NOT NULL,
  scopes_json TEXT NOT NULL,
  reviewed_by TEXT NOT NULL,
  review_ref TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_governed_servers_active ON governed_servers(revoked_at);
"""
# --- end Horizon 2 connector governance ---------------------------------------

# --- graph planning canvas (task #807) -----------------------------------------
# Excalidraw planning canvas persistence: one scene per board id. The scene is
# the verbatim Excalidraw elements JSON the browser edits; agents read it via
# `graph_canvas_read` and parse text labels into task/dependency SUGGESTIONS.
# No sketch ever executes anything — proposals only, through the approval queue.
_GRAPH_CANVAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_canvas_scenes (
  board_id TEXT PRIMARY KEY,
  scene_json TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
# --- end graph planning canvas (task #807) ------------------------------------


# The FTS-update trigger is scoped to `OF title, content` so that the
# access_count/last_accessed bumps in memory_search/memory_get (a hot recall
# path) do NOT re-index FTS on every read. Owned by _migrate() rather than
# _SCHEMA so a pre-existing DB with the old unscoped trigger is recreated exactly
# once (CREATE TRIGGER IF NOT EXISTS would otherwise never replace it).
_MEMORY_AU_TRIGGER = """
CREATE TRIGGER memory_au AFTER UPDATE OF title, content ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
  INSERT INTO memory_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def next_revision(current: str | None) -> str:
    """A task ``updated_at`` strictly newer than ``current``, at microsecond precision.

    Every task status writer stamps this, so two transitions in one second get
    distinct revisions and a later one never sorts before an earlier one (the
    completion-return key, task #2047)."""
    candidate = datetime.now(UTC)
    if current:
        try:
            previous = datetime.fromisoformat(current)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            if candidate <= previous:
                candidate = previous + timedelta(microseconds=1)
        except ValueError:
            pass
    return candidate.isoformat(timespec="microseconds")


def begin_task_write(conn: sqlite3.Connection) -> None:
    """Hold the writer lock from a task revision read through its write.

    A plain SELECT takes no writer lock, so without this another connection
    can commit a revision between the read and the UPDATE and both writes
    stamp the same ``next_revision`` (task #2061 review). Starts
    ``BEGIN IMMEDIATE`` unless the connection already has a transaction: in
    legacy isolation that means a DML statement already ran and holds the
    write lock, or the caller opened its own. The caller commits or rolls
    back as it already does."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def next_task_revision(conn: sqlite3.Connection, task_id: int) -> str:
    """``next_revision`` for one task, read on the caller's connection.

    For writers that change a task row they have not read (a release, a
    reopen, a status flip). Takes the writer lock first (``begin_task_write``),
    so the revision read and the caller's UPDATE are one atomic step and the
    stamp is strictly after every committed revision (task #2061). A missing
    row yields a plain current revision."""
    begin_task_write(conn)
    row = conn.execute("SELECT updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    return next_revision(row[0] if row else None)


def get_launchd_first_seen(label: str) -> str | None:
    """First-observed-loaded-with-zero-runs timestamp for a LaunchAgent label,
    or None if this process has never recorded one. See launchd_first_seen's
    schema comment for why this exists instead of trusting the plist mtime."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT first_seen_at FROM launchd_first_seen WHERE label=?", (label,)
        ).fetchone()
    finally:
        conn.close()
    return row["first_seen_at"] if row is not None else None


def record_launchd_first_seen(label: str, first_seen_at: str) -> None:
    """Set a label's first-seen timestamp once; a later call is a no-op.

    Deliberately never updated after the first write — the whole point is an
    anchor that a subsequent, unrelated plist rewrite (a redeploy that
    doesn't bootout+bootstrap the label) cannot silently reset.
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO launchd_first_seen(label, first_seen_at) VALUES (?, ?)",
            (label, first_seen_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_zulip_cursor(agent_label: str, channel: str) -> dict[str, object] | None:
    """Return the durable cursor for one Zulip bot/channel pair, if present."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT agent_label, channel, last_message_id, updated_at "
            "FROM zulip_cursors WHERE agent_label=? AND channel=?",
            (agent_label, channel),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def list_zulip_cursors() -> list[dict[str, object]]:
    """Return all durable Zulip cursors in stable agent/channel order."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_label, channel, last_message_id, updated_at "
            "FROM zulip_cursors ORDER BY agent_label, channel"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def save_zulip_cursor(agent_label: str, channel: str, last_message_id: int) -> dict[str, object]:
    """Advance a Zulip cursor monotonically after successful message handling.

    The upsert refuses to move a cursor backwards.  Callers persist the event
    first and invoke this function second, so a crash between the two operations
    replays safely through the event log's idempotency key.
    """
    if not isinstance(agent_label, str) or not agent_label.strip():
        raise ValueError("agent_label must be a non-empty string")
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel must be a non-empty string")
    if isinstance(last_message_id, bool) or not isinstance(last_message_id, int):
        raise ValueError("last_message_id must be an integer")
    if last_message_id < 0:
        raise ValueError("last_message_id must be non-negative")

    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_cursors "
            "(agent_label, channel, last_message_id, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent_label, channel) DO UPDATE SET "
            "last_message_id = MAX(zulip_cursors.last_message_id, excluded.last_message_id), "
            "updated_at = CASE WHEN excluded.last_message_id > zulip_cursors.last_message_id "
            "THEN excluded.updated_at ELSE zulip_cursors.updated_at END",
            (agent_label.strip(), channel.strip(), last_message_id, timestamp),
        )
        conn.commit()
        row = conn.execute(
            "SELECT agent_label, channel, last_message_id, updated_at "
            "FROM zulip_cursors WHERE agent_label=? AND channel=?",
            (agent_label.strip(), channel.strip()),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def get_zulip_queue(agent_label: str) -> dict[str, object] | None:
    """Return durable event-queue state for one bot identity."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT agent_label, queue_id, last_event_id, registered_at, last_event_at, "
            "status, last_error, next_retry_at, reset_count, updated_at "
            "FROM zulip_event_queues WHERE agent_label=?",
            (agent_label,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def list_zulip_queues() -> list[dict[str, object]]:
    """Return durable event-queue state in stable identity order."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_label, queue_id, last_event_id, registered_at, last_event_at, "
            "status, last_error, next_retry_at, reset_count, updated_at "
            "FROM zulip_event_queues ORDER BY agent_label"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def save_zulip_queue(
    agent_label: str,
    queue_id: str,
    last_event_id: int,
    *,
    registered_at: str | None = None,
    status: str = "healthy",
    reset_count: int | None = None,
) -> dict[str, object]:
    """Create or replace queue identity state after successful registration."""
    timestamp = now()
    registered_at = registered_at or timestamp
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_event_queues "
            "(agent_label, queue_id, last_event_id, registered_at, last_event_at, "
            "status, last_error, next_retry_at, reset_count, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?) "
            "ON CONFLICT(agent_label) DO UPDATE SET queue_id=excluded.queue_id, "
            "last_event_id=excluded.last_event_id, registered_at=excluded.registered_at, "
            "status=excluded.status, last_error=NULL, next_retry_at=NULL, "
            "reset_count=COALESCE(?, zulip_event_queues.reset_count), updated_at=excluded.updated_at",
            (
                agent_label.strip(),
                queue_id.strip(),
                last_event_id,
                registered_at,
                status,
                reset_count if reset_count is not None else 0,
                timestamp,
                reset_count,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_queue(agent_label)
    assert row is not None
    return row


def advance_zulip_queue(agent_label: str, last_event_id: int) -> dict[str, object]:
    """Advance an event cursor only after its events were durably handled."""
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "UPDATE zulip_event_queues SET last_event_id=MAX(last_event_id, ?), "
            "last_event_at=?, status='healthy', last_error=NULL, next_retry_at=NULL, updated_at=? "
            "WHERE agent_label=?",
            (last_event_id, timestamp, timestamp, agent_label),
        )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_queue(agent_label)
    if row is None:
        raise RuntimeError(f"Zulip queue state missing for {agent_label}")
    return row


def record_zulip_queue_error(
    agent_label: str,
    error: str,
    *,
    status: str = "error",
    reset: bool = False,
    next_retry_at: str | None = None,
    clear_queue: bool = False,
) -> dict[str, object]:
    """Record queue degradation without storing credentials or message content.

    When ``clear_queue`` is True, drop ``queue_id`` so the next poll must
    re-register and re-run REST backfill before consuming events.
    """
    timestamp = now()
    safe_error = str(error)[:500]
    conn = connect()
    try:
        if clear_queue:
            conn.execute(
                "INSERT INTO zulip_event_queues "
                "(agent_label, queue_id, last_event_id, status, last_error, next_retry_at, "
                "reset_count, updated_at) "
                "VALUES (?, NULL, -1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_label) DO UPDATE SET queue_id=NULL, last_event_id=-1, "
                "status=excluded.status, last_error=excluded.last_error, "
                "next_retry_at=excluded.next_retry_at, "
                "reset_count=zulip_event_queues.reset_count + ?, "
                "updated_at=excluded.updated_at",
                (
                    agent_label,
                    status,
                    safe_error,
                    next_retry_at,
                    1 if reset else 0,
                    timestamp,
                    1 if reset else 0,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO zulip_event_queues "
                "(agent_label, queue_id, last_event_id, status, last_error, next_retry_at, "
                "reset_count, updated_at) "
                "VALUES (?, NULL, -1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_label) DO UPDATE SET status=excluded.status, "
                "last_error=excluded.last_error, next_retry_at=excluded.next_retry_at, "
                "reset_count=zulip_event_queues.reset_count + ?, "
                "updated_at=excluded.updated_at",
                (
                    agent_label,
                    status,
                    safe_error,
                    next_retry_at,
                    1 if reset else 0,
                    timestamp,
                    1 if reset else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_queue(agent_label)
    assert row is not None
    return row


def get_zulip_bridge_cursor(name: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT cursor FROM zulip_bridge_state WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    return row["cursor"] if row is not None else None


def save_zulip_bridge_cursor(name: str, cursor: str) -> None:
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_bridge_state(name, cursor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
            (name, cursor, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def count_recent_zulip_notifications(since: str) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM zulip_outbound_notifications "
            "WHERE status='sent' AND sent_at>=?",
            (since,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["count"] if row else 0)


def get_zulip_notification(dedupe_key: str) -> dict[str, object] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT dedupe_key, kind, agent_label, channel, topic, status, sent_at, error, "
            "created_at, updated_at FROM zulip_outbound_notifications WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def record_zulip_notification(
    dedupe_key: str,
    *,
    kind: str,
    agent_label: str,
    channel: str,
    topic: str,
    status: str,
    error: str | None = None,
) -> dict[str, object]:
    timestamp = now()
    sent_at = timestamp if status == "sent" else None
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_outbound_notifications "
            "(dedupe_key, kind, agent_label, channel, topic, status, sent_at, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET status=excluded.status, sent_at=excluded.sent_at, "
            "error=excluded.error, updated_at=excluded.updated_at",
            (
                dedupe_key,
                kind,
                agent_label,
                channel,
                topic,
                status,
                sent_at,
                str(error)[:500] if error else None,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_notification(dedupe_key)
    assert row is not None
    return row


def claim_zulip_chat_reply(
    inbound_message_id: int,
    *,
    agent_label: str,
    channel: str,
    topic: str,
) -> bool:
    """Atomically claim one inbound message for a chat reply. Returns True on win."""
    if isinstance(inbound_message_id, bool) or not isinstance(inbound_message_id, int):
        raise ValueError("inbound_message_id must be an integer")
    if inbound_message_id < 0:
        raise ValueError("inbound_message_id must be non-negative")
    timestamp = now()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO zulip_chat_replies "
            "(inbound_message_id, agent_label, channel, topic, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'claimed', ?, ?)",
            (inbound_message_id, agent_label, channel, topic, timestamp, timestamp),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def finish_zulip_chat_reply(
    inbound_message_id: int,
    *,
    status: str,
    reply_message_id: int | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    """Update a claimed chat reply row after send/skip/failure."""
    if status not in ("sent", "failed", "skipped"):
        raise ValueError("status must be sent, failed, or skipped")
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "UPDATE zulip_chat_replies SET status=?, reply_message_id=?, error=?, updated_at=? "
            "WHERE inbound_message_id=?",
            (
                status,
                reply_message_id,
                str(error)[:500] if error else None,
                timestamp,
                inbound_message_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT inbound_message_id, agent_label, channel, topic, status, reply_message_id, "
            "error, created_at, updated_at FROM zulip_chat_replies WHERE inbound_message_id=?",
            (inbound_message_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def get_zulip_chat_reply(inbound_message_id: int) -> dict[str, object] | None:
    """Return one chat-reply row by inbound Zulip message ID, or None."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT inbound_message_id, agent_label, channel, topic, status, reply_message_id, "
            "error, created_at, updated_at FROM zulip_chat_replies WHERE inbound_message_id=?",
            (inbound_message_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def count_recent_zulip_chat_replies(since: str) -> int:
    """Count successfully sent chat replies at or after ``since`` (ISO timestamp)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM zulip_chat_replies "
            "WHERE status='sent' AND updated_at>=?",
            (since,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["count"] if row else 0)


def bind_zulip_chat_topic(channel: str, topic: str, agent_label: str) -> None:
    """Remember which bot owns a topic for follow-up messages without [target:]."""
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_chat_topics(channel, topic, agent_label, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel, topic) DO UPDATE SET agent_label=excluded.agent_label, "
            "updated_at=excluded.updated_at",
            (channel, topic, agent_label, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def get_zulip_chat_topic(channel: str, topic: str) -> dict[str, object] | None:
    """Return the agent bound to a Zulip channel/topic for chat continuity, or None."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT channel, topic, agent_label, updated_at FROM zulip_chat_topics "
            "WHERE channel=? AND topic=?",
            (channel, topic),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def claim_slack_chat_reply(
    inbound_event_id: str,
    *,
    agent_label: str,
    conversation_id: str,
    channel_id: str,
    cap: int | None = None,
    cutoff: str | None = None,
) -> tuple[bool, str]:
    """Atomically claim one Slack event for a live-chat reply under an optional hourly cap.

    Returns ``(claimed, status)`` where status is ``'ok'``, ``'already-claimed'``,
    or ``'rate-limited'``.
    """
    key = (inbound_event_id or "").strip()
    if not key:
        raise ValueError("inbound_event_id must be non-empty")
    timestamp = now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT inbound_event_id, status FROM slack_chat_replies WHERE inbound_event_id=?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "failed":
                conn.execute(
                    "DELETE FROM slack_chat_replies WHERE inbound_event_id=?",
                    (key,),
                )
            else:
                conn.commit()
                return False, "already-claimed"
        if cap is not None and cutoff is not None:
            count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM slack_chat_replies "
                "WHERE (status='sent' OR status='claimed') AND created_at>=?",
                (cutoff,),
            ).fetchone()
            if count_row and int(count_row["count"]) >= cap:
                conn.commit()
                return False, "rate-limited"
        conn.execute(
            "INSERT INTO slack_chat_replies "
            "(inbound_event_id, agent_label, conversation_id, channel_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'claimed', ?, ?)",
            (key, agent_label, conversation_id, channel_id, timestamp, timestamp),
        )
        conn.commit()
        return True, "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_stale_slack_chat_claims(
    *,
    max_age_s: float = 300.0,
    action: str = "fail",
    cutoff: str | None = None,
) -> list[dict[str, object]]:
    """Recover or fail in-flight 'claimed' chat replies that survived a worker/hub restart."""
    timestamp = now()
    effective_cutoff = cutoff or (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat(
        timespec="seconds"
    )
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT inbound_event_id, agent_label, conversation_id, channel_id, status, "
            "reply_ts, error, created_at, updated_at FROM slack_chat_replies "
            "WHERE status='claimed' AND created_at<=?",
            (effective_cutoff,),
        ).fetchall()
        recovered = [dict(r) for r in rows]
        if recovered:
            if action == "fail":
                conn.execute(
                    "UPDATE slack_chat_replies SET status='failed', "
                    "error='worker-restarted-or-timed-out', updated_at=? "
                    "WHERE status='claimed' AND created_at<=?",
                    (timestamp, effective_cutoff),
                )
            conn.commit()
        return recovered
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_slack_chat_reply(
    inbound_event_id: str,
    *,
    status: str,
    reply_ts: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    """Update a claimed Slack chat reply row after send/skip/failure."""
    if status not in ("sent", "failed", "skipped"):
        raise ValueError("status must be sent, failed, or skipped")
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "UPDATE slack_chat_replies SET status=?, reply_ts=?, error=?, updated_at=? "
            "WHERE inbound_event_id=?",
            (
                status,
                reply_ts,
                str(error)[:500] if error else None,
                timestamp,
                inbound_event_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT inbound_event_id, agent_label, conversation_id, channel_id, status, "
            "reply_ts, error, created_at, updated_at FROM slack_chat_replies "
            "WHERE inbound_event_id=?",
            (inbound_event_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


SLACK_CHAT_QUEUE_OVERLOAD_ERROR = "queue-overloaded"


def get_slack_chat_reply(inbound_event_id: str) -> dict[str, object] | None:
    """Return one Slack live-chat reply row by inbound Slack ``event_id``, or None."""
    key = (inbound_event_id or "").strip()
    if not key:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT inbound_event_id, agent_label, conversation_id, channel_id, status, "
            "reply_ts, error, created_at, updated_at FROM slack_chat_replies "
            "WHERE inbound_event_id=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def record_slack_chat_schedule_failure(
    inbound_event_id: str,
    *,
    channel_id: str,
    conversation_id: str | None = None,
    error: str = SLACK_CHAT_QUEUE_OVERLOAD_ERROR,
) -> None:
    """Persist a failed chat row when background scheduling refuses the inbound event."""
    key = (inbound_event_id or "").strip()
    if not key:
        raise ValueError("inbound_event_id must be non-empty")
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO slack_chat_replies "
            "(inbound_event_id, agent_label, conversation_id, channel_id, status, "
            "reply_ts, error, created_at, updated_at) VALUES "
            "(?, 'unscheduled', ?, ?, 'failed', NULL, ?, ?, ?)",
            (
                key,
                conversation_id or "",
                channel_id,
                str(error)[:500],
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def slack_chat_schedule_failure_retryable(inbound_event_id: str) -> bool:
    """Return True when a prior overload failure should accept a Slack retry."""
    key = (inbound_event_id or "").strip()
    if not key:
        return False
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, error FROM slack_chat_replies WHERE inbound_event_id=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return row["status"] == "failed" and row["error"] == SLACK_CHAT_QUEUE_OVERLOAD_ERROR


def clear_slack_chat_reply(inbound_event_id: str) -> None:
    """Remove a chat reply row so a retried Slack event can be scheduled again."""
    key = (inbound_event_id or "").strip()
    if not key:
        return
    conn = connect()
    try:
        conn.execute("DELETE FROM slack_chat_replies WHERE inbound_event_id=?", (key,))
        conn.commit()
    finally:
        conn.close()


def count_recent_slack_chat_replies(since: str) -> int:
    """Count successfully sent Slack chat replies at or after ``since``."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM slack_chat_replies "
            "WHERE status='sent' AND updated_at>=?",
            (since,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["count"] if row else 0)


def bind_slack_chat_conversation(
    conversation_id: str, *, agent_label: str, channel_id: str
) -> None:
    """Remember which bot owns a Slack thread for follow-ups without ``[target:]``."""
    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO slack_chat_conversations"
            "(conversation_id, agent_label, channel_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "agent_label=excluded.agent_label, channel_id=excluded.channel_id, "
            "updated_at=excluded.updated_at",
            (conversation_id, agent_label, channel_id, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def get_slack_chat_conversation(conversation_id: str) -> dict[str, object] | None:
    """Return the agent bound to a Slack conversation key, or None."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT conversation_id, agent_label, channel_id, updated_at "
            "FROM slack_chat_conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def get_zulip_worker_health(agent_label: str, channel: str) -> dict[str, object] | None:
    """Return durable health for one Zulip worker channel."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT agent_label, channel, status, last_poll_at, last_success_at, last_error, "
            "messages_processed, consecutive_errors, next_retry_at, updated_at "
            "FROM zulip_worker_health WHERE agent_label=? AND channel=?",
            (agent_label, channel),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def list_zulip_worker_health() -> list[dict[str, object]]:
    """Return durable health rows in stable agent/channel order."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_label, channel, status, last_poll_at, last_success_at, last_error, "
            "messages_processed, consecutive_errors, next_retry_at, updated_at "
            "FROM zulip_worker_health ORDER BY agent_label, channel"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def record_zulip_worker_success(
    agent_label: str,
    channel: str,
    *,
    messages_processed: int,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Record a successful poll without storing any credential material."""
    if isinstance(messages_processed, bool) or not isinstance(messages_processed, int):
        raise ValueError("messages_processed must be an integer")
    if messages_processed < 0:
        raise ValueError("messages_processed must be non-negative")
    timestamp = timestamp or now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_worker_health "
            "(agent_label, channel, status, last_poll_at, last_success_at, last_error, "
            "messages_processed, consecutive_errors, next_retry_at, updated_at) "
            "VALUES (?, ?, 'healthy', ?, ?, NULL, ?, 0, NULL, ?) "
            "ON CONFLICT(agent_label, channel) DO UPDATE SET "
            "status='healthy', last_poll_at=excluded.last_poll_at, "
            "last_success_at=excluded.last_success_at, last_error=NULL, "
            "messages_processed=zulip_worker_health.messages_processed + excluded.messages_processed, "
            "consecutive_errors=0, next_retry_at=NULL, updated_at=excluded.updated_at",
            (agent_label, channel, timestamp, timestamp, messages_processed, timestamp),
        )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_worker_health(agent_label, channel)
    assert row is not None
    return row


def record_zulip_worker_error(
    agent_label: str,
    channel: str,
    *,
    error: str,
    next_retry_at: str,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Record a bounded worker error and its next retry deadline."""
    timestamp = timestamp or now()
    safe_error = str(error)[:500]
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO zulip_worker_health "
            "(agent_label, channel, status, last_poll_at, last_success_at, last_error, "
            "messages_processed, consecutive_errors, next_retry_at, updated_at) "
            "VALUES (?, ?, 'error', ?, NULL, ?, 0, 1, ?, ?) "
            "ON CONFLICT(agent_label, channel) DO UPDATE SET "
            "status='error', last_poll_at=excluded.last_poll_at, last_error=excluded.last_error, "
            "consecutive_errors=zulip_worker_health.consecutive_errors + 1, "
            "next_retry_at=excluded.next_retry_at, updated_at=excluded.updated_at",
            (agent_label, channel, timestamp, safe_error, next_retry_at, timestamp),
        )
        conn.commit()
    finally:
        conn.close()
    row = get_zulip_worker_health(agent_label, channel)
    assert row is not None
    return row


# SQLite file header (https://www.sqlite.org/fileformat.html). A non-matching
# prefix is how we distinguish the desktop hub-retirement text stub from a
# real memory.db once SQLite raises the opaque "file is not a database"
# DatabaseError that flooded the 2026-08-22 review cron inbox.
#
# The header is read only AFTER SQLite has reported SQLITE_NOTADB, never as a
# preflight (task #1637). POSIX advisory locks belong to the process, and
# closing ANY descriptor on a file releases all of them. A Python open/read/
# close of a live memory.db therefore silently drops the SHARED lock held by
# every other connection in this process ("How To Corrupt An SQLite Database
# File", section 2.2). Another process can then take EXCLUSIVE at close,
# decide it is the last connection, checkpoint, and delete -wal/-shm under the
# running hub, whose next connect fails with "disk I/O error". A file SQLite
# refuses to read has no live connections, so reading it then is safe.
_SQLITE_HEADER = b"SQLite format 3\x00"
_NO_LOCAL_MEMORY_DB_MARKER = "NO_LOCAL_MEMORY_DB"


class LiveStoreError(RuntimeError):
    """Raised when ``memory.db`` is missing, retired, or not a usable task store.

    Review/CLI entry points convert this to exit code 2 (configuration error)
    so cron never surfaces the historical ``no such table: tasks`` /
    ``file is not a database`` strings as review failures.
    """


def require_disposable_synthetic_home() -> Path:
    """Fail closed before *any* store access while disposable acceptance is on.

    This also guards wrapper activity/performance writes, not only the tool body.
    It is an operator opt-in for a private local fixture, not a sandbox against
    another process with arbitrary same-user filesystem access.
    """
    if os.environ.get("UA_SYNTHETIC_WORKFLOW") != "1":
        raise LiveStoreError("synthetic workflow is disabled")
    for name, expected in (
        ("UA_STATE_SYNC", "0"),
        ("UA_EVENT_BUS", "0"),
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
    ):
        if os.environ.get(name) != expected:
            raise LiveStoreError(
                "synthetic acceptance requires sync/event bus off and model acquisition offline"
            )
    if any(
        os.environ.get(name, "").strip()
        for name in ("UA_APPROVAL_PUSH_URL", "UA_APPROVAL_WEBHOOK_URL")
    ):
        raise LiveStoreError("outbound approval hooks must be disabled for synthetic acceptance")
    home = Path(os.path.abspath(config.data_home()))
    if any(part.is_symlink() for part in (home, *home.parents)) or not home.is_dir():
        raise LiveStoreError("synthetic home must be an unlinked existing directory")
    marker = home / "DISPOSABLE_SYNTHETIC_WORKFLOW"
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LiveStoreError("regular unlinked disposable marker required")
        if marker.read_text() != "disposable-synthetic-workflow\n":
            raise LiveStoreError("invalid disposable synthetic home marker")
    except OSError as exc:
        raise LiveStoreError("explicit disposable synthetic home marker required") from exc
    for name in ("memory.db", "synthetic-provider.sqlite"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = home / (name + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LiveStoreError("linked or nonregular synthetic database path refused")
    return home


def _guard_disposable_synthetic_store() -> None:
    if os.environ.get("UA_SYNTHETIC_WORKFLOW") == "1":
        require_disposable_synthetic_home()


def no_local_memory_db_marker() -> Path:
    """Path of the host-local marker that says this machine must not own memory.db."""

    return config.data_home() / _NO_LOCAL_MEMORY_DB_MARKER


def require_live_task_store() -> Path:
    """Fail closed unless ``UA_HOME/memory.db`` is a real SQLite DB with ``tasks``.

    Historical desktop failure modes after the Mac hub cutover (task #561):

    - *Missing* ``memory.db``: ``sqlite3.connect`` creates an empty file, then
      the first ``SELECT … FROM tasks`` raises ``no such table: tasks``.
    - *Text stub* at the ``memory.db`` path (retirement pointer written as the
      database filename): ``PRAGMA journal_mode=WAL`` raises
      ``file is not a database``.
    - ``NO_LOCAL_MEMORY_DB`` marker present: this host deliberately retired its
      local store; running the review cron here must not open anything under
      ``UA_HOME``.

    Returns the verified absolute path. Never creates or mutates the file.
    """
    marker = no_local_memory_db_marker()
    if marker.is_file():
        raise LiveStoreError(
            f"local task store retired ({marker.name} present under {config.data_home()}); "
            "run unified-agent-review only on the authoritative hub host, or unset "
            "UA_HOME if it points at a retired data directory"
        )

    _guard_disposable_synthetic_store()
    path = Path(config.db_path())
    if not path.is_file():
        raise LiveStoreError(
            f"task store missing at {path}; refusing to auto-create an empty SQLite "
            "file that would fail later with 'no such table: tasks'"
        )

    # No Python-level header preflight: it would release this process's
    # SQLite locks on a live store (see _SQLITE_HEADER). SQLite reads the
    # header itself; the stub is identified only after it reports NOTADB.
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LiveStoreError(f"task store at {path} failed to open read-only: {exc}") from exc
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        if _is_not_a_database(exc):
            header = _read_rejected_header(path, "task store")
            raise LiveStoreError(
                f"task store at {path} is not a SQLite database "
                f"(header={header!r}); refusing open that would fail with "
                "'file is not a database' — check UA_HOME points at the live hub store"
            ) from exc
        raise LiveStoreError(
            f"task store unreadable at {path}: {_describe_sqlite_error(exc)}"
        ) from exc
    try:
        if row is None:
            raise LiveStoreError(
                f"task store at {path} has no 'tasks' table "
                "(wrong or uninitialized database); check UA_HOME"
            )
    finally:
        conn.close()
    return path.resolve()


def _is_not_a_database(exc: sqlite3.Error) -> bool:
    """True when SQLite refused the file itself (SQLITE_NOTADB)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF == sqlite3.SQLITE_NOTADB
    return "file is not a database" in str(exc)


def _describe_sqlite_error(exc: sqlite3.Error) -> str:
    """Error text plus SQLite's extended code name (e.g. SQLITE_IOERR_SHORT_READ)."""
    name = getattr(exc, "sqlite_errorname", None)
    return f"{exc} ({name})" if name else str(exc)


def _read_rejected_header(path: Path, what: str) -> bytes:
    """Header bytes of a file SQLite has already rejected as NOTADB.

    Safe only on that path: no SQLite connection holds locks on a file SQLite
    cannot read, so closing this descriptor releases nothing (see
    _SQLITE_HEADER). Never call it as a preflight on a live store.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER))
    except OSError as exc:
        raise LiveStoreError(f"{what} unreadable at {path}: {exc}") from exc


def _unusable_store_error(path: Path, exc: sqlite3.DatabaseError) -> LiveStoreError:
    """The LiveStoreError for a store SQLite opened but could not use.

    A regular file SQLite rejects as NOTADB is the desktop tombstone (task
    #509) and gets the actionable message. Anything else keeps SQLite's own
    text plus its extended code name so the next failure is diagnosable.
    """
    if _is_not_a_database(exc) and path.is_file():
        header = _read_rejected_header(path, "memory.db")
        return LiveStoreError(
            f"memory.db at {path} is not a SQLite database (header={header!r}); "
            "check UA_HOME. On remote desktop clients/executors, shared task and "
            "memory state must come from the Mac hub, not a local tombstone file."
        )
    return LiveStoreError(f"memory.db at {path} is unusable: {_describe_sqlite_error(exc)}")


def _process_owns(path: Path) -> bool:
    """True when this process can chmod ``path`` because it owns it."""
    try:
        return path.stat().st_uid == os.geteuid()
    except OSError:
        return False


def _shared_data_directory(path: Path) -> bool:
    """True when ``path`` is a shared directory, not a dedicated app home.

    ``UA_HOME=/tmp`` under root must not become mode 0700. A sticky
    world-writable directory, a filesystem root, and a home directory are
    shared. So is any directory that already grants write to the group or
    to others, such as an owner-created ``/srv/ua-team`` at mode 0770.
    A dedicated app home (``0700`` or ``0755``) has neither write bit and
    can be tightened. ``~/.unified_agent`` is not shared.
    """
    try:
        resolved = path.resolve()
        mode = resolved.stat().st_mode
    except OSError:
        return True
    if resolved == Path(resolved.anchor):
        return True
    if mode & 0o1000 and mode & 0o002:
        return True
    # Group or other write means this directory is already shared. Do not
    # silently take that access away. A private home has neither bit.
    if mode & 0o022:
        return True
    shared = {
        Path("/tmp"),
        Path("/var"),
        Path("/var/tmp"),
        Path("/private/tmp"),
        Path("/home"),
        Path("/root"),
        Path("/usr"),
        Path("/opt"),
        Path("/Users"),
        Path("/Users/Shared"),
        Path("/dev/shm"),
    }
    if resolved in shared:
        return True
    try:
        home = Path.home().resolve()
    except RuntimeError:
        return False
    return resolved == home or resolved == home.parent


def _require_private_store_parent(path: Path) -> None:
    """Refuse a database whose directory lets another user replace it.

    File mode 0600 does not protect a SQLite store when another user can
    replace ``memory.db`` or its WAL sidecars through the parent directory.
    Check before opening the database so no untrusted replacement is read.
    """
    parent = path.parent
    try:
        mode = parent.stat().st_mode
    except FileNotFoundError:
        return  # sqlite3.connect retains its existing missing-directory error.
    if mode & 0o022:
        raise LiveStoreError(
            f"memory.db parent {parent} is group- or world-writable; "
            "set UA_HOME to a private directory before opening the store; "
            f"run: chmod 700 {parent}"
        )


def _restrict_store_permissions(path: Path) -> None:
    """Owner-only mode for the store, its directory, and WAL sidecars.

    Called only after ``sqlite3.connect`` has succeeded, so a directory sitting
    at the db path still raises bare ``sqlite3.Error`` from ``connect`` itself.
    ``-wal`` / ``-shm`` are tightened when SQLite has already created them.

    chmod runs only for paths this process owns. A group-writable store owned
    by someone else stays usable: chmod would raise ``PermissionError`` after
    the connection was opened. An owned path that still cannot be tightened
    raises, and ``connect`` closes the handle before returning that error.
    The containing directory is tightened only when it is a dedicated app
    directory. A writable shared parent is refused before connection.
    """
    if path.is_file() and _process_owns(path):
        os.chmod(path, 0o600)
    parent = path.parent
    if parent.is_dir() and _process_owns(parent) and not _shared_data_directory(parent):
        os.chmod(parent, 0o700)
    for suffix in ("-wal", "-shm"):
        sibling = path.with_name(path.name + suffix)
        if sibling.is_file() and _process_owns(sibling):
            os.chmod(sibling, 0o600)


def connect_read_only() -> sqlite3.Connection:
    """Read an initialized local store without bootstrap, migration, or chmod.

    Application SQL is read-only: URI mode prevents creating memory.db and
    query_only rejects accidental metadata writes. SQLite's normal WAL reader
    may still create/write -wal and -shm coordination files. A fully non-writable
    directory without those files is unsupported and reports store unavailable.
    Keep WAL coordination enabled to see a live writer's committed WAL data;
    immutable mode would bypass that authority. The advisory timeout is bounded.
    """
    _guard_disposable_synthetic_store()
    path = Path(config.db_path())
    if no_local_memory_db_marker().is_file():
        raise LiveStoreError("local metadata store retired; shared state lives on the hub")
    if not path.is_file():
        raise LiveStoreError("local metadata store missing or unreadable")
    _require_private_store_parent(path)
    conn = None
    try:
        conn = org_partition.open_store(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            raise LiveStoreError("local metadata store has no schema version")
        try:
            version = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise LiveStoreError("local metadata store has invalid schema metadata") from exc
        if not 0 < version <= SCHEMA_VERSION:
            raise LiveStoreError("local metadata store has an incompatible schema version")
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise LiveStoreError("local metadata store unavailable for read-only access") from exc
    except Exception:
        if conn is not None:
            conn.close()
        raise


def connect(*, check_same_thread: bool = True, scoped: bool = True) -> sqlite3.Connection:
    """Open the live store.

    ``check_same_thread=False`` is for a caller that hands one connection
    between worker threads strictly one at a time (the live-state poller).

    On a store an operator has partitioned (task #1433) the result is an
    ``org_partition.ScopedConnection``. ``scoped=False`` is for migrations and
    the backfill itself, which must see the stored ``org_id`` values.
    """
    _guard_disposable_synthetic_store()
    path = Path(config.db_path())
    # A host that retired its local store (the NO_LOCAL_MEMORY_DB marker, set on
    # every machine that is a client of the Mac hub) must never open, and so
    # never create, memory.db. Without this, the first import that touched
    # storage grew a fresh empty store (2026-09-27, Linux desktop).
    marker = no_local_memory_db_marker()
    if marker.is_file():
        raise LiveStoreError(
            f"local task store retired ({marker.name} present under {config.data_home()}); "
            "this host is a hub client — shared state lives on the hub, not in a local memory.db"
        )
    _require_private_store_parent(path)
    # Deliberate: no wrapper here. sqlite3.connect's own error (e.g. a
    # DIRECTORY at the db path) must propagate as bare sqlite3.Error — the
    # durable-failure contract test_record_event_reports_a_durable_failure
    # pins that. The regular-file tombstone is recognised only after SQLite
    # rejects it below, never by a Python-level header preflight: that would
    # release the POSIX locks of this process's live connections (task #1637,
    # see _SQLITE_HEADER). The daemon's reclaim guard catches either error.
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    try:
        edition.ensure_store_tenant(conn)
        if org_partition.is_partitioned(conn):
            org_partition.refuse_tenant()
            if scoped:
                conn.close()
                conn = sqlite3.connect(
                    path,
                    check_same_thread=check_same_thread,
                    factory=org_partition.ScopedConnection,
                )
                conn.row_factory = sqlite3.Row
    except BaseException as exc:
        conn.close()
        if isinstance(exc, edition.TenantStoreMismatchError):
            raise LiveStoreError(str(exc)) from exc
        if isinstance(exc, sqlite3.DatabaseError):
            raise _unusable_store_error(path, exc) from exc
        raise
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise _unusable_store_error(path, exc) from exc
    from .vector_math import cosine_similarity_bytes

    conn.create_function("cosine_similarity", 2, cosine_similarity_bytes)

    # sqlite-vec (optional accelerated KNN for memory_search/memory_save's
    # dedup check — System Leverage Plan T1-2): best-effort, same posture as
    # fastembed itself. Extension loading is per-connection (unlike the
    # memory_vec table, which persists in the DB file once created), so this
    # must run on every connect(), not just once at startup. A platform
    # without a compatible wheel, or a Python build without
    # enable_load_extension support, degrades silently to the
    # cosine_similarity() UDF scan registered above — callers never see the
    # difference except in latency.
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass

    try:
        _restrict_store_permissions(path)
    except OSError as exc:
        conn.close()
        raise LiveStoreError(
            f"memory.db permissions at {path} could not be restricted: {exc}"
        ) from exc
    # Which tenant this process stamps here, for system_doctor (task #1996).
    edition.record_tenant_beacon(path.parent)
    return conn


# fastembed's fixed default model (BAAI/bge-small-en-v1.5) output size. Only
# relevant if the memory_vec fast path is ever pointed at a different
# embedding model — this would need to become a migration, not a constant.
MEMORY_VEC_DIM = 384


def _ensure_memory_vec_table(conn: sqlite3.Connection) -> None:
    """Create the memory_vec accelerated KNN index if sqlite-vec loaded on
    this connection. Best-effort: an OperationalError here (extension not
    loaded) just means memory_search/memory_save keep using the
    cosine_similarity() UDF scan — never raised, never fatal to init_db()."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING "
            f"vec0(embedding float[{MEMORY_VEC_DIM}] distance_metric=cosine)"
        )
    except sqlite3.OperationalError:
        pass


def _ensure_memory_chunk_vec_table(conn: sqlite3.Connection) -> None:
    """Create the chunk-vector mirror if sqlite-vec loaded (task #2182).

    Same posture as ``memory_vec``: a failure leaves chunk search on the
    ``cosine_similarity()`` scan of ``memory_chunks``. Keyed by
    ``memory_chunks.rowid``, not by the note.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunk_vec USING "
            f"vec0(embedding float[{MEMORY_VEC_DIM}] distance_metric=cosine)"
        )
    except sqlite3.OperationalError:
        pass


def upsert_memory_vec(conn: sqlite3.Connection, note_id: str, embedding: bytes) -> None:
    """Best-effort mirror write into memory_vec, keyed by memory.rowid.

    vec0 tables support UPDATE but not INSERT OR REPLACE (raises a UNIQUE
    constraint error), so this is a manual upsert: UPDATE first, INSERT only
    if no row existed. A no-op if memory_vec doesn't exist on this
    connection (sqlite-vec unavailable) or the note itself doesn't exist —
    memory_embeddings (the source of truth) is unaffected either way, and
    memory_search/memory_save's dedup check always fall back to it."""
    try:
        row = conn.execute("SELECT rowid FROM memory WHERE id=?", (note_id,)).fetchone()
        if row is None:
            return
        rowid = row["rowid"]
        cur = conn.execute("UPDATE memory_vec SET embedding=? WHERE rowid=?", (embedding, rowid))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)", (rowid, embedding)
            )
    except Exception:
        pass


def invalidate_memory_embedding(conn: sqlite3.Connection, note_id: str) -> None:
    """Remove a note's source embedding and accelerated mirror row.

    A note update changes the text represented by its vector. If replacement
    generation fails, retaining the old row would make coverage report a false
    all-clear and let semantic search return results for stale content. Both
    deletes are best-effort so the fail-soft memory-save contract remains true
    even when sqlite-vec is unavailable or damaged.
    """
    try:
        conn.execute("DELETE FROM memory_embeddings WHERE note_id=?", (note_id,))
    except Exception:
        pass
    try:
        row = conn.execute("SELECT rowid FROM memory WHERE id=?", (note_id,)).fetchone()
        if row is not None:
            conn.execute("DELETE FROM memory_vec WHERE rowid=?", (row["rowid"],))
    except Exception:
        pass


@contextmanager
def _memory_cleanup_savepoint(conn: sqlite3.Connection, *, write: bool = True):
    # Keep the caller's transaction open; failures must not leave half a cleanup.
    if write and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT memory_cleanup")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO memory_cleanup")
        raise
    finally:
        conn.execute("RELEASE memory_cleanup")


def _has_memory_vec(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_vec' AND type='table'"
        ).fetchone()
        is not None
    )


def _has_sqlite_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (name,)
        ).fetchone()
        is not None
    )


def delete_memory_chunks(conn: sqlite3.Connection, note_id: str) -> None:
    """Delete one note's chunk rows and their vec0 mirror, without committing.

    The mirror is keyed by ``memory_chunks.rowid``. Those rowids are reusable,
    so the mirror rows are removed first (task #1815, the same bug as
    ``memory_vec``). An absent chunk table or mirror is fine. A mirror that
    exists but cannot be deleted raises, so a hard delete cannot silently
    strand a vector. The caller owns the transaction.
    """
    if not _has_sqlite_table(conn, "memory_chunks"):
        return
    rowids = conn.execute("SELECT rowid FROM memory_chunks WHERE note_id=?", (note_id,)).fetchall()
    if rowids and _has_sqlite_table(conn, "memory_chunk_vec"):
        conn.executemany("DELETE FROM memory_chunk_vec WHERE rowid=?", rowids)
    conn.execute("DELETE FROM memory_chunks WHERE note_id=?", (note_id,))


def hard_delete_memory(conn: sqlite3.Connection, note_id: str, *, org_id: str | None) -> bool:
    """Delete a tenant-owned note and its auxiliary rows without committing.

    Delete the vec0 mirror before the memory rowid can be reused. An absent
    optional vector table is fine; an existing but unusable table is an error,
    so a hard delete cannot silently strand a vector. Source embeddings cascade
    through the existing foreign key. Chunk rows cascade too, but their vec0
    mirror does not, so it is deleted first. The caller owns commit/rollback.
    """
    with _memory_cleanup_savepoint(conn):
        row = conn.execute(
            "SELECT rowid FROM memory WHERE id=? AND org_id IS ?", (note_id, org_id)
        ).fetchone()
        if row is None:
            return False
        delete_memory_chunks(conn, note_id)
        if _has_memory_vec(conn):
            conn.execute("DELETE FROM memory_vec WHERE rowid=?", (row["rowid"],))
        conn.execute("DELETE FROM memory_recall_votes WHERE note_id=?", (note_id,))
        conn.execute("DELETE FROM memory WHERE id=? AND org_id IS ?", (note_id, org_id))
    return True


def repair_memory_orphans(
    conn: sqlite3.Connection, *, apply: bool = False
) -> dict[str, int | bool]:
    """Preview or remove legacy memory auxiliary orphans in an initialized store.

    Local maintenance only, across all tenants; not exposed as an agent tool or
    called on startup. A reused memory rowid is not proof its old vector is
    valid: compare the mirror to the current note's source embedding too.
    Notes, source embeddings and votes for existing notes remain intact. Apply
    leaves the transaction open for the explicitly authorized caller to commit.
    """
    with _memory_cleanup_savepoint(conn, write=apply):
        vectors = []
        if _has_memory_vec(conn):
            vectors = conn.execute(
                "SELECT v.rowid FROM memory_vec v "
                "LEFT JOIN memory m ON m.rowid=v.rowid "
                "LEFT JOIN memory_embeddings e ON e.note_id=m.id "
                "WHERE m.id IS NULL OR e.note_id IS NULL OR v.embedding != e.embedding"
            ).fetchall()
        missing_note = "NOT EXISTS (SELECT 1 FROM memory m WHERE m.id=memory_recall_votes.note_id)"
        votes = conn.execute(
            f"SELECT COUNT(*) FROM memory_recall_votes WHERE {missing_note}"
        ).fetchone()[0]
        chunk_vectors = []
        if _has_sqlite_table(conn, "memory_chunk_vec") and _has_sqlite_table(conn, "memory_chunks"):
            chunk_vectors = conn.execute(
                "SELECT v.rowid FROM memory_chunk_vec v "
                "LEFT JOIN memory_chunks c ON c.rowid = v.rowid "
                "WHERE c.rowid IS NULL OR v.embedding != c.embedding"
            ).fetchall()
        if apply:
            if vectors:
                conn.executemany("DELETE FROM memory_vec WHERE rowid=?", vectors)
            if chunk_vectors:
                conn.executemany("DELETE FROM memory_chunk_vec WHERE rowid=?", chunk_vectors)
            conn.execute(f"DELETE FROM memory_recall_votes WHERE {missing_note}")
    return {
        "memory_vec": len(vectors),
        "memory_chunk_vec": len(chunk_vectors),
        "memory_recall_votes": votes,
        "applied": apply,
    }


def _backfill_agent_registry(conn: sqlite3.Connection) -> None:
    """Seed `agents` once from the label columns that already exist.

    Runs at the END of _migrate, after every ALTER TABLE above has guaranteed the
    source columns (tasks.claimed_by, memory.author, ...) are present.

    Guarded on COUNT(*)==0 so it fires exactly once per database, and every insert
    is INSERT OR IGNORE so two machines racing a synced memory.db cannot corrupt
    each other. Crucially this must never *reset* last_seen on an already-populated
    registry — a re-run would otherwise drag every agent backwards to whenever it
    last appeared in the historical tables.
    """
    seeded = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
    if seeded:
        return

    # (table, label column, timestamp column). Labels here are historical and
    # unvalidated by design: live data contains 'unified-agent-cron' and
    # 'Desktop_Agent1 (steward)', which are not in config.agent_labels(). The
    # registry is a mirror of what the cluster actually did, not a gatekeeper.
    sources = (
        ("agent_events", "agent", "ts"),
        ("event_log", "originator", "created_at"),
        ("tasks", "claimed_by", "claimed_at"),
        ("delegations", "from_agent", "created_at"),
        ("delegations", "to_agent", "created_at"),
        ("memory", "author", "updated_at"),
        ("approvals", "requested_by", "created_at"),
    )

    seen: dict[str, tuple[str | None, str | None]] = {}
    for table, label_col, ts_col in sources:
        try:
            rows = conn.execute(
                f"SELECT {label_col} AS label, MIN({ts_col}) AS first, MAX({ts_col}) AS last "
                f"FROM {table} WHERE {label_col} IS NOT NULL AND TRIM({label_col}) != '' "
                f"GROUP BY {label_col}"
            ).fetchall()
        except sqlite3.Error:
            # A table or column this build does not have. Backfill is a nicety;
            # missing history must never stop a startup.
            continue
        for row in rows:
            label = str(row["label"]).strip()
            prev_first, prev_last = seen.get(label, (None, None))
            first, last = row["first"], row["last"]
            # Timestamps are ISO-8601 text throughout, so min/max are lexicographic.
            if first and (prev_first is None or first < prev_first):
                prev_first = first
            if last and (prev_last is None or last > prev_last):
                prev_last = last
            seen[label] = (prev_first, prev_last)

    if not seen:
        return

    ts = now()
    conn.executemany(
        "INSERT OR IGNORE INTO agents"
        " (label, kind, known, first_seen, last_seen, last_seen_via, created_at, updated_at)"
        " VALUES (?, 'unknown', ?, ?, ?, 'backfill', ?, ?)",
        [
            (label, 1 if config.is_known_agent_label(label) else 0, first, last, ts, ts)
            for label, (first, last) in sorted(seen.items())
        ],
    )


def _widen_executor_attempts_task_type(conn: sqlite3.Connection) -> None:
    """Rebuild ``executor_attempts`` so its task_type CHECK also allows 'scout'.

    SQLite cannot ALTER a CHECK. The new CREATE statement is *derived from the
    stored SQL* by replacing only the task_type value list, so every column,
    default, constraint and foreign key the table has today (including columns
    later migrations added with ALTER TABLE) is carried over without a
    hand-copied column list that could drift. Gated on the stored SQL lacking
    ``'scout'``, so it runs at most once per DB; a table whose SQL does not have
    exactly one recognisable task_type CHECK is left alone.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='executor_attempts'"
    ).fetchone()
    old_sql = (row["sql"] or "") if row else ""
    if not old_sql or "'scout'" in old_sql:
        return
    check_re = re.compile(r"(task_type\s+IN\s*\()([^)]*)(\))", re.IGNORECASE)
    if len(check_re.findall(old_sql)) != 1:
        return
    name_re = re.compile(
        r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?executor_attempts["`]?',
        re.IGNORECASE,
    )
    if not name_re.match(old_sql):
        return

    def _add_scout(m: re.Match[str]) -> str:
        values = m.group(2)
        if "'research'" in values:
            values = values.replace("'research'", "'research','scout'", 1)
        else:
            values = values.rstrip() + ",'scout'"
        return m.group(1) + values + m.group(3)

    new_sql = check_re.sub(_add_scout, old_sql, count=1)
    new_sql = name_re.sub("CREATE TABLE executor_attempts__rebuild", new_sql, count=1)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(executor_attempts)").fetchall()]
    col_list = ", ".join(cols)
    # Dropping the table takes its indexes/triggers with it; capture their
    # stored definitions (auto-indexes have NULL sql) and replay them.
    extras = [
        r["sql"]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='executor_attempts' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY rowid"
        ).fetchall()
    ]
    # Keep AUTOINCREMENT's high-water mark (the copy would reset it to the
    # highest surviving id, letting a deleted attempt id be reused).
    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'executor_attempts'"
    ).fetchone()
    high_water = int(seq_row["seq"]) if seq_row is not None else 0
    recreate = ";\n            ".join(extras)
    if recreate:
        recreate += ";"
    # One explicit transaction (same idiom as the agent_tokens rebuild): an
    # error between the copy and the rename rolls back to the original table.
    # foreign_keys=OFF so DROP TABLE cannot fire ON DELETE actions into
    # referencing rows; PRAGMA foreign_keys only changes outside a transaction.
    try:
        conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS executor_attempts__rebuild;
            {new_sql};
            INSERT INTO executor_attempts__rebuild ({col_list})
              SELECT {col_list} FROM executor_attempts ORDER BY id;
            DROP TABLE executor_attempts;
            ALTER TABLE executor_attempts__rebuild RENAME TO executor_attempts;
            {recreate}
            UPDATE sqlite_sequence SET seq = MAX(seq, {high_water})
              WHERE name = 'executor_attempts';
            INSERT INTO sqlite_sequence (name, seq)
              SELECT 'executor_attempts', {high_water}
              WHERE {high_water} > 0
                AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'executor_attempts');
            COMMIT;
            """
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _add_approval_run_id(conn: sqlite3.Connection) -> None:
    """Nullable ``approvals.run_id`` (task #1653), idempotently.

    Display linkage only: which run was current when the request was queued.
    No default, no CHECK, and no backfill — a row that predates the column
    stays NULL. Not a SCHEMA_VERSION bump. The call sits after the approvals
    status rebuild so an ancient database that still takes that path gets the
    column on the final table.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    if "run_id" not in cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN run_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id)")


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, additive column migrations for pre-existing shared DBs.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so columns added
    after a DB was first created must be back-filled here. The shared memory.db
    is used across machines/IDEs, so this must be safe to run on every startup.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
    if "author" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN author TEXT")
    if "updated_by" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN updated_by TEXT")
    if "salience" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN salience REAL NOT NULL DEFAULT 0.5")
    if "confidence" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN confidence REAL")
    if "last_accessed" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN last_accessed TEXT")
    if "access_count" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
    # source_host: audit-trail column naming the machine that originated a note.
    # Additive and nullable — existing rows read back as NULL (unknown origin).
    if "source_host" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN source_host TEXT")

    event_cols = {r["name"] for r in conn.execute("PRAGMA table_info(event_log)").fetchall()}
    if "dedupe_key" not in event_cols:
        conn.execute("ALTER TABLE event_log ADD COLUMN dedupe_key TEXT")
    if "prev_hash" not in event_cols:
        conn.execute("ALTER TABLE event_log ADD COLUMN prev_hash TEXT")
    if "entry_hash" not in event_cols:
        conn.execute("ALTER TABLE event_log ADD COLUMN entry_hash TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_dedupe ON event_log(dedupe_key)")

    # task #919: idx_agent_perf_tool served no query (all perf readers filter by
    # ts first) yet had grown to ~80 MB on the shared hub. Drop it everywhere.
    conn.execute("DROP INDEX IF EXISTS idx_agent_perf_tool")

    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "source_host" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_host TEXT")
    if "claimed_by" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN claimed_by TEXT")
    if "claimed_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN claimed_at TEXT")
    if "lock_expires_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN lock_expires_at TEXT")
    if "untrusted" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN untrusted INTEGER NOT NULL DEFAULT 0")
    if "worktree_path" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN worktree_path TEXT")
    if "branch" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN branch TEXT")
    if "archived_at" not in task_cols:
        # Additive visibility metadata. Existing rows remain unarchived until
        # an explicit, human-approved disposition backfills them.
        conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
    if "project" not in task_cols:
        # First-class project-scoped task streams (task #327). Every pre-existing
        # row predates project separation and belongs to the original single
        # queue, so the column default backfills them as 'unified-agent' rather
        # than leaving a NULL that every project filter would then have to
        # special-case.
        conn.execute("ALTER TABLE tasks ADD COLUMN project TEXT NOT NULL DEFAULT 'unified-agent'")
    # Runs every init_db(), not just inside the ALTER branch above: a fresh DB's
    # CREATE TABLE already has the column (see _SCHEMA) but never got the index.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project)")
    if "domain" not in task_cols:
        # Portfolio-domain classification (task #373 Tier 1, 2.3): explicit tag
        # at creation, defaulting by project, overridable — see
        # _PROJECT_DEFAULT_DOMAIN in tools/tasks.py. Every pre-existing row
        # predates domain separation, and every current project value maps to
        # 'hobby' (task #339's Hobby Builder bucket is exactly the four
        # existing PROJECTS), so that is the safe backfill default rather than
        # a NULL every portfolio-UI query would then have to special-case.
        conn.execute("ALTER TABLE tasks ADD COLUMN domain TEXT NOT NULL DEFAULT 'hobby'")
    # Same reasoning as idx_tasks_project above: only safe here, after the ALTER.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain)")
    if "evidence_ref" not in task_cols:
        # Optional pointer (commit SHA, PR URL, CI run link) backing a `done`
        # transition on a `[code-task]`-marked task — see
        # _evidence_required_error in tools/tasks.py. Nullable: only tasks that
        # opt in via the marker are held to this bar, so no default is needed.
        conn.execute("ALTER TABLE tasks ADD COLUMN evidence_ref TEXT")
    if "due_at" not in task_cols:
        # Light commitment/reminder semantics mapped onto tasks (task #373
        # Tier 1, 2.2), per the approved Personal OS spec's "commitment is a
        # task with a due-flavored date, not a separate store" decision
        # (Wiki/Personal-OS.md). The spec's own draft named this concept
        # `expiry_or_review` (borrowed from #342's memory-fact staleness
        # field) but flagged it as unverified — no such column existed
        # anywhere. This is that field, implemented for tasks under a clearer
        # name: an ISO date/datetime a task/commitment is due or should be
        # reviewed by. Nullable — most tasks have no due date.
        conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_at ON tasks(due_at)")

    # tasks.lane (UA Task Queue V2.0 / task #705): routing lane with CHECK enum via
    # table rebuild — SQLite cannot add a CHECK column with ALTER. Backfill
    # lane='default' for every row; notes-based laptop signals become lane='laptop'.
    task_cols_lane = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "lane" not in task_cols_lane:
        lane_check = config.task_lane_sql_check()
        select_evidence = (
            "evidence_ref" if "evidence_ref" in task_cols_lane else "NULL AS evidence_ref"
        )
        conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE tasks_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              notes TEXT,
              status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','doing','waiting_review','blocked','done','cancelled')),
              priority TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
              parent_id INTEGER REFERENCES tasks_new(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              archived_at TEXT,
              source_host TEXT,
              claimed_by TEXT,
              claimed_at TEXT,
              lock_expires_at TEXT,
              untrusted INTEGER NOT NULL DEFAULT 0,
              worktree_path TEXT,
              branch TEXT,
              project TEXT NOT NULL DEFAULT 'unified-agent',
              domain TEXT NOT NULL DEFAULT 'hobby',
              due_at TEXT,
              evidence_ref TEXT,
              lane TEXT NOT NULL DEFAULT 'default' {lane_check}
            );
            INSERT INTO tasks_new
              SELECT id, title, notes, status, priority, parent_id, created_at, updated_at,
                     completed_at, archived_at, source_host, claimed_by, claimed_at,
                     lock_expires_at, untrusted, worktree_path, branch, project, domain,
                     due_at, {select_evidence},
                     CASE
                       WHEN instr(lower(COALESCE(notes, '')), 'laptop-lane') > 0
                         OR instr(lower(COALESCE(notes, '')), 'laptop-main') > 0
                         OR instr(lower(COALESCE(notes, '')), '[waiting-for: laptop-dev-claim]') > 0
                       THEN 'laptop'
                       ELSE 'default'
                     END
              FROM tasks ORDER BY id;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
            CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain);
            CREATE INDEX IF NOT EXISTS idx_tasks_due_at ON tasks(due_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_lane ON tasks(lane);
            PRAGMA foreign_keys=ON;
            """
        )

    # tasks.lane actor model (task #883): `lane` used to double as a portfolio
    # bucket (personal / professional / hobby), duplicating `domain`. Those are
    # no longer valid lanes — collapse them to the untriaged 'default' pool and
    # let a triage pass route them to dispatch-ready / operator-only / tracking.
    # `domain` already carries the bucket, so nothing is lost. Idempotent: the
    # WHERE matches nothing once the remap has run.
    legacy_lanes = ", ".join("?" for _ in config.TASK_LEGACY_LANES)
    conn.execute(
        f"UPDATE tasks SET lane = 'default' WHERE lane IN ({legacy_lanes})",
        config.TASK_LEGACY_LANES,
    )

    # #1716: canonicalize the retired operator actor lane. Only this column
    # changes; leases, run bindings, timestamps and immutable events survive.
    conn.execute(
        "UPDATE tasks SET lane='operator-only' WHERE lane=?",
        (config.LEGACY_OPERATOR_LANE,),
    )

    cycle_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(autonomous_cycles)").fetchall()
    }
    cycle_defaults = (
        ("max_time_s", "REAL NOT NULL DEFAULT 3600"),
        ("max_tokens", "INTEGER NOT NULL DEFAULT 100000"),
        ("max_retries", "INTEGER NOT NULL DEFAULT 3"),
        ("max_cost_cents", "REAL NOT NULL DEFAULT 1000"),
        ("used_time_s", "REAL NOT NULL DEFAULT 0"),
        ("used_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("used_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("used_cost_cents", "REAL NOT NULL DEFAULT 0"),
        ("allowed_capabilities", "TEXT"),
    )
    for name, definition in cycle_defaults:
        if name not in cycle_cols:
            conn.execute(f"ALTER TABLE autonomous_cycles ADD COLUMN {name} {definition}")

    # autonomous_cycles.state CHECK migration (task #373 Tier 2, 3.3): 'killed'
    # is a new terminal state, distinct from 'blocked' (soft-stop, resumable
    # from 'plan') and 'failed' (execution failure) — a kill switch needs a
    # state that reads unambiguously as "a human/agent forcibly stopped this,"
    # never confusable with a budget-exceeded block or a natural failure. Same
    # SQLite CHECK-rebuild pattern as the delegations/approvals/tasks status
    # migrations above, guarded on the old constraint text so it runs once.
    cycles_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='autonomous_cycles'"
    ).fetchone()
    if cycles_sql and "'killed'" not in (cycles_sql["sql"] or ""):
        conn.executescript(
            """
            CREATE TABLE autonomous_cycles_new (
              id TEXT PRIMARY KEY,
              task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
              plan_id TEXT,
              delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
              session_id TEXT,
              state TEXT NOT NULL DEFAULT 'plan'
                CHECK (state IN ('plan','delegated','executing','validating','reviewing',
                                 'rework','pr','blocked','completed','failed','killed')),
              max_rework INTEGER NOT NULL DEFAULT 2 CHECK (max_rework >= 0),
              rework_count INTEGER NOT NULL DEFAULT 0 CHECK (rework_count >= 0),
              max_time_s REAL NOT NULL DEFAULT 3600 CHECK (max_time_s > 0),
              max_tokens INTEGER NOT NULL DEFAULT 100000 CHECK (max_tokens > 0),
              max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries > 0),
              max_cost_cents REAL NOT NULL DEFAULT 1000 CHECK (max_cost_cents > 0),
              used_time_s REAL NOT NULL DEFAULT 0 CHECK (used_time_s >= 0),
              used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
              used_retries INTEGER NOT NULL DEFAULT 0 CHECK (used_retries >= 0),
              used_cost_cents REAL NOT NULL DEFAULT 0 CHECK (used_cost_cents >= 0),
              allowed_capabilities TEXT,
              stop_reason TEXT,
              pr_url TEXT,
              version INTEGER NOT NULL DEFAULT 0,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO autonomous_cycles_new
              SELECT id, task_id, plan_id, delegation_id, session_id, state, max_rework,
                     rework_count, max_time_s, max_tokens, max_retries, max_cost_cents,
                     used_time_s, used_tokens, used_retries, used_cost_cents,
                     allowed_capabilities, stop_reason, pr_url, version, created_by,
                     created_at, updated_at
              FROM autonomous_cycles;
            DROP TABLE autonomous_cycles;
            ALTER TABLE autonomous_cycles_new RENAME TO autonomous_cycles;
            CREATE INDEX IF NOT EXISTS idx_autonomous_cycles_state ON autonomous_cycles(state);
            CREATE INDEX IF NOT EXISTS idx_autonomous_cycles_task ON autonomous_cycles(task_id);
            """
        )

    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN superseded_by TEXT")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN expires_at TEXT")
    if "feedback_helpful" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN feedback_helpful INTEGER NOT NULL DEFAULT 0")
    if "feedback_not_helpful" not in cols:
        conn.execute(
            "ALTER TABLE memory ADD COLUMN feedback_not_helpful INTEGER NOT NULL DEFAULT 0"
        )
    if "feedback_stale" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN feedback_stale INTEGER NOT NULL DEFAULT 0")
    if "feedback_wrong" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN feedback_wrong INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory(expires_at)")
    # Evidence-backed memory (task #1459): whether a note states an observed
    # fact or an inference, the supporting refs (JSON list such as
    # "session:<id>", "task:<id>"), when the claim became true, and notes it
    # contradicts without superseding them. Additive and nullable: legacy rows
    # read back as unlabeled, no evidence. Columns are added here rather than
    # in the CREATE TABLE so the org_id comment caveat above stays untouched.
    if "epistemic" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN epistemic TEXT")
    if "evidence" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN evidence TEXT")
    if "valid_from" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN valid_from TEXT")
    if "contradicts" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN contradicts TEXT")
    # Bi-temporal validity (task #2095): when the claim stopped being true.
    # Stamped by a supersede (invalidate, never delete) and cleared by a
    # restore. Additive and nullable: NULL means still valid, and a legacy
    # superseded row has no recorded end.
    if "valid_to" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN valid_to TEXT")

    # plan_steps.outputs (task 62): JSON map a finishing agent leaves for the next
    # step's owner — the inter-agent payload of a chained workflow. Additive; old
    # rows read back as '{}' (no outputs recorded).
    step_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plan_steps)").fetchall()}
    if "outputs" not in step_cols:
        conn.execute("ALTER TABLE plan_steps ADD COLUMN outputs TEXT NOT NULL DEFAULT '{}'")

    # approvals.status CHECK migration (task #171): 'revoked' and 'expired' are
    # new terminal states — an approved-but-unconsumed pass can now be withdrawn
    # by a human, and goes stale on its own after UA_APPROVAL_PASS_TTL_HOURS.
    # Both must be distinguishable from 'denied' and 'consumed' in the audit
    # trail, so reusing an existing status is not an option. SQLite cannot ALTER
    # a CHECK constraint, hence the table rebuild — guarded on the old
    # constraint text so it runs at most once per DB.
    approvals_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='approvals'"
    ).fetchone()
    if approvals_sql and "'revoked'" not in (approvals_sql["sql"] or ""):
        conn.executescript(
            """
            CREATE TABLE approvals_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tool TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}',
              requested_by TEXT,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','denied','consumed','revoked','expired')),
              reason TEXT,
              resolved_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_at TEXT
            );
            INSERT INTO approvals_new
              SELECT id, tool, payload, requested_by, status, reason, resolved_by,
                     created_at, updated_at, resolved_at
              FROM approvals;
            DROP TABLE approvals;
            ALTER TABLE approvals_new RENAME TO approvals;
            CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
            CREATE INDEX IF NOT EXISTS idx_approvals_tool ON approvals(tool);
            """
        )

    # After the rebuild above (task #1653). See _add_approval_run_id.
    _add_approval_run_id(conn)

    # delegations columns & status CHECK migration (task #179): add attempts,
    # last_attempt_at, and 'failed' terminal status.
    del_cols = {r["name"] for r in conn.execute("PRAGMA table_info(delegations)").fetchall()}
    if "attempts" not in del_cols:
        conn.execute("ALTER TABLE delegations ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "last_attempt_at" not in del_cols:
        conn.execute("ALTER TABLE delegations ADD COLUMN last_attempt_at TEXT")

    delegations_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='delegations'"
    ).fetchone()
    if delegations_sql and "'failed'" not in (delegations_sql["sql"] or ""):
        conn.executescript(
            """
            CREATE TABLE delegations_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              from_agent TEXT NOT NULL,
              to_agent TEXT NOT NULL,
              task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              task_spec TEXT NOT NULL,
              deadline TEXT,
              callback TEXT NOT NULL DEFAULT 'notify'
                CHECK (callback IN ('notify','memory','none')),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','in_flight','completed','cancelled','failed')),
              memory_id TEXT,
              callback_result TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_attempt_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );
            INSERT INTO delegations_new
              SELECT id, from_agent, to_agent, task_id, task_spec, deadline, callback,
                     status, memory_id, callback_result, attempts, last_attempt_at,
                     created_at, updated_at, completed_at
              FROM delegations;
            DROP TABLE delegations;
            ALTER TABLE delegations_new RENAME TO delegations;
            CREATE INDEX IF NOT EXISTS idx_delegations_task ON delegations(task_id);
            CREATE INDEX IF NOT EXISTS idx_delegations_to_agent ON delegations(to_agent);
            CREATE INDEX IF NOT EXISTS idx_delegations_status ON delegations(status);
            """
        )

    # tasks.status CHECK migration (task lifecycle contract, docs/task-lifecycle-contract.md
    # §1.4 / §6 Q5-Q6, approved by the operator 2026-08-08): 'waiting_review' replaces the
    # `[waiting-for: review]` notes marker (reviewer.py/monitor.py/daemon.py keep reading
    # the marker too during the transition — see the consumer updates alongside this
    # migration) and 'cancelled' is a new terminal state distinct from 'done'. SQLite
    # cannot ALTER a CHECK constraint, hence the table rebuild, guarded on the old
    # constraint text so it runs at most once per DB. `ORDER BY id` on the copy matters
    # here (unlike the approvals/delegations rebuilds above): tasks.parent_id is a
    # self-referential FK with foreign_keys=ON (connect()), so a parent row must be
    # inserted before any child row that references it within the same INSERT.
    tasks_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if tasks_sql and "'cancelled'" not in (tasks_sql["sql"] or ""):
        # This rebuild fires on any pre-#171 DB, which may also predate the
        # `project`/`domain`/`due_at` columns (tasks #327, #373) — the ALTER
        # TABLE ADD COLUMN statements above have already run by this point in
        # _migrate, but a plain `SELECT *`-style column list here would
        # silently drop them again on rebuild, so all three are carried
        # through explicitly rather than assumed absent.
        rebuild_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        select_project = "project" if "project" in rebuild_cols else "'unified-agent' AS project"
        select_domain = "domain" if "domain" in rebuild_cols else "'hobby' AS domain"
        select_due_at = "due_at" if "due_at" in rebuild_cols else "NULL AS due_at"
        conn.executescript(
            f"""
            CREATE TABLE tasks_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              notes TEXT,
              status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','doing','waiting_review','blocked','done','cancelled')),
              priority TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
              parent_id INTEGER REFERENCES tasks_new(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              archived_at TEXT,
              source_host TEXT,
              claimed_by TEXT,
              claimed_at TEXT,
              lock_expires_at TEXT,
              untrusted INTEGER NOT NULL DEFAULT 0,
              worktree_path TEXT,
              branch TEXT,
              project TEXT NOT NULL DEFAULT 'unified-agent',
              domain TEXT NOT NULL DEFAULT 'hobby',
              due_at TEXT
            );
            INSERT INTO tasks_new
              SELECT id, title, notes, status, priority, parent_id, created_at, updated_at,
                     completed_at, archived_at, source_host, claimed_by, claimed_at,
                     lock_expires_at, untrusted, worktree_path, branch, {select_project},
                     {select_domain}, {select_due_at}
              FROM tasks ORDER BY id;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            """
        )

    # web_search_cache table migration
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_search_cache (
          provider TEXT NOT NULL,
          query TEXT NOT NULL,
          max_results INTEGER NOT NULL,
          results TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          PRIMARY KEY (provider, query, max_results)
        )
        """
    )

    # vault_ingest_ledger table migration (see _SCHEMA for rationale)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_ingest_ledger (
          path TEXT PRIMARY KEY,
          content_hash TEXT NOT NULL,
          ingested_at TEXT NOT NULL,
          agent TEXT,
          pages TEXT NOT NULL DEFAULT '[]',
          reason TEXT
        )
        """
    )
    ingest_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(vault_ingest_ledger)").fetchall()
    }
    if "reason" not in ingest_cols:
        # Optional human note recorded alongside the actor attribution (task
        # #1157, Proposals pane dismiss reason). Additive and nullable —
        # pre-existing rows read back as NULL (no reason captured).
        conn.execute("ALTER TABLE vault_ingest_ledger ADD COLUMN reason TEXT")

    # idempotency_keys (task #429). Same constant init_db applies, so a
    # database created before that block still gets CREATE TABLE IF NOT EXISTS
    # on this path. Columns are unchanged.
    _apply_idempotency_schema(conn)

    # executor_attempts.task_id/delegation_id CASCADE -> SET NULL migration
    # (task #467 / PR #201 review): the table shipped hours earlier with
    # CASCADE, which let task_delete (or a delegation's cascade) erase
    # completed execution evidence and retroactively change trust-ladder
    # counts. SQLite can't ALTER a foreign key's ON DELETE action, hence the
    # rebuild — gated on the stored SQL so it runs at most once per DB, same
    # pattern as the delegations/tasks rebuilds above.
    attempts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='executor_attempts'"
    ).fetchone()
    if attempts_sql and "ON DELETE CASCADE" in (attempts_sql["sql"] or ""):
        conn.executescript(
            """
            CREATE TABLE executor_attempts_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
              delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
              agent TEXT NOT NULL,
              backend TEXT NOT NULL,
              task_type TEXT NOT NULL DEFAULT 'unknown'
                CHECK (task_type IN ('code','docs','research','review','operations','unknown')),
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT,
              outcome TEXT NOT NULL DEFAULT 'unknown'
                CHECK (outcome IN ('success','failure','blocked','quarantined','cooldown','killed','waiting_review','unknown')),
              detail TEXT
            );
            INSERT INTO executor_attempts_new
              SELECT id, task_id, delegation_id, agent, backend, task_type,
                     started_at, finished_at, status, outcome, detail
              FROM executor_attempts;
            DROP TABLE executor_attempts;
            ALTER TABLE executor_attempts_new RENAME TO executor_attempts;
            CREATE INDEX IF NOT EXISTS idx_executor_attempts_task ON executor_attempts(task_id);
            CREATE INDEX IF NOT EXISTS idx_executor_attempts_delegation ON executor_attempts(delegation_id);
            CREATE INDEX IF NOT EXISTS idx_executor_attempts_outcome ON executor_attempts(outcome);
            """
        )

    # task #1081: classify recorded failures (provider quota vs iteration budget
    # vs code error) so model_quota_status can derive lane availability.
    dispatch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(dispatch_runs)").fetchall()}
    if dispatch_cols and "failure_class" not in dispatch_cols:
        conn.execute("ALTER TABLE dispatch_runs ADD COLUMN failure_class TEXT")
    # Task #1221: distinguish cancellation recorded from cessation confirmed.
    # SQLite ALTER TABLE ADD COLUMN cannot attach a CHECK constraint to an
    # existing table, so validation for this column lives in coordinator.py.
    if dispatch_cols and "termination_outcome" not in dispatch_cols:
        conn.execute("ALTER TABLE dispatch_runs ADD COLUMN termination_outcome TEXT")
    # Task #1221 finding 1: widen the live-slot index to also cover a
    # cancelled row whose cessation is not confirmed. `CREATE INDEX IF NOT
    # EXISTS` is a no-op when an index by this name already exists under the
    # OLD (narrower) definition, so a pre-existing DB needs an explicit
    # drop-and-recreate here — this runs on every migrate() call and is
    # idempotent once the new definition is in place.
    if dispatch_cols:
        expected_index_sql = (
            f"CREATE UNIQUE INDEX idx_dispatch_runs_live_task "
            f"ON dispatch_runs(task_id) "
            f"WHERE {DISPATCH_RUN_LIVE_SLOT_PREDICATE}"
        )
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_dispatch_runs_live_task'"
        ).fetchone()
        existing_sql = (existing["sql"] or "").strip() if existing else ""

        def _norm_sql(s: str) -> str:
            return " ".join(s.split()).lower().replace('"', "").replace("`", "")

        if not existing_sql or _norm_sql(existing_sql) != _norm_sql(expected_index_sql):
            if existing_sql:
                conn.execute("DROP INDEX IF EXISTS idx_dispatch_runs_live_task")
            try:
                conn.execute(expected_index_sql)
            except sqlite3.IntegrityError as exc:
                # Never fall back to the old narrower index: doing so would let a
                # legacy cancelled/unknown row coexist with a new live run and
                # silently weaken the slot invariant. Refuse startup until the
                # conflicting legacy rows are reconciled by an operator.
                # Task #1221 finding 7: report the conflicting task and run IDs.
                conflicts = conn.execute(
                    f"SELECT task_id, GROUP_CONCAT(run_id) AS run_ids, COUNT(*) AS n "
                    f"FROM dispatch_runs WHERE {DISPATCH_RUN_LIVE_SLOT_PREDICATE} "
                    f"GROUP BY task_id HAVING n > 1"
                ).fetchall()
                if conflicts:
                    detail = ", ".join(
                        f"task {c['task_id']}: runs [{c['run_ids']}]" for c in conflicts
                    )
                    msg = (
                        f"cannot install fail-closed dispatch live-slot index: "
                        f"conflicting live runs for tasks ({detail})"
                    )
                else:
                    msg = (
                        "cannot install fail-closed dispatch live-slot index: "
                        "legacy rows conflict with an unconfirmed cancellation"
                    )
                raise sqlite3.IntegrityError(msg) from exc
    attempt_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(executor_attempts)").fetchall()
    }
    if attempt_cols and "failure_class" not in attempt_cols:
        conn.execute("ALTER TABLE executor_attempts ADD COLUMN failure_class TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_runs_failure_class ON dispatch_runs(failure_class)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_attempts_failure_class "
        "ON executor_attempts(failure_class)"
    )

    # task #1153: surface the model id used for a run on session + evidence rows.
    session_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()}
    if session_cols and "model" not in session_cols:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN model TEXT")

    attempt_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(executor_attempts)").fetchall()
    }
    if attempt_cols and "model" not in attempt_cols:
        conn.execute("ALTER TABLE executor_attempts ADD COLUMN model TEXT")

    # task #1543: widen executor_attempts.task_type's CHECK with 'scout'. The
    # daemon records scout runs as task_type='scout' (evidence.TASK_TYPES), but
    # the CHECK only allowed the older vocabulary, so start_attempt's INSERT
    # raised and scout runs left no trust-ladder evidence. Placed AFTER the
    # CASCADE rebuild (which hardcodes the narrow list) and after every
    # ADD COLUMN on this table, so it rebuilds from the table as it stands now.
    _widen_executor_attempts_task_type(conn)

    # Add missing performance indexes (task #162)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_lane ON tasks(lane)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_superseded ON memory(superseded_by)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_events_agent_kind_ts ON agent_events(agent, kind, ts)"
    )

    # Registry indexes, declared a second time on purpose. They already exist in
    # _AGENT_REGISTRY_SCHEMA, which runs before this function — but a future table
    # rebuild landing above would silently drop them, and a single init_db() (what a
    # service restart does) would leave them missing. That is precisely the #178
    # defect. Re-asserting them here costs nothing and closes the hole permanently.
    for name, target in _AGENT_REGISTRY_INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")

    _backfill_agent_registry(conn)

    # agent_tokens table migration (task #1198, token lifecycle as a platform
    # feature). Hash-only by construction: no column here can hold a plaintext
    # bearer value, only a digest produced by managed_secrets.set_secret's
    # existing digest computation. `rotated_from` self-references the row a
    # rotation superseded, so a chain of rotations is reconstructable without
    # ever having stored the values that were rotated.
    #
    # NOTE for whoever reconciles this against task #1232 (Epic B3, in flight
    # concurrently on storage.py): this block is intentionally additive and
    # self-contained (its own CREATE TABLE IF NOT EXISTS + indexes, no shared
    # column edits), so it does not need reordering relative to #1232's org_id
    # migration. #1232 claimed SCHEMA_VERSION 30 first (PR #659, not yet
    # merged), so this file's SCHEMA_VERSION is 31 here — a one-line bump,
    # kept easy to renumber again if merge order changes. Positioned after
    # every CHECK-constraint table rebuild in this function (tasks/approvals/
    # delegations above) on purpose: those rebuilds copy hardcoded column
    # lists that predate a column added below them, so a step landing above
    # one would be silently dropped on the next rebuild (see #1232's org_id
    # migration, which hit exactly this and now runs last for the same
    # reason). This block adds no column to any of those three tables, but
    # keeping it last costs nothing and avoids relearning the lesson.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_label TEXT NOT NULL,
          token_hash TEXT NOT NULL,
          hash_algo TEXT NOT NULL DEFAULT 'sha256',
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'rotated', 'revoked', 'expired')),
          issued_at TEXT NOT NULL,
          expires_at TEXT,
          rotated_from INTEGER REFERENCES agent_tokens(id) ON DELETE SET NULL,
          rotated_at TEXT,
          revoked_at TEXT,
          issued_by TEXT,
          note TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tokens_label_status "
        "ON agent_tokens(agent_label, status)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tokens_issued_at ON agent_tokens(issued_at)")

    # work_products table migration (task #1278, H2: the non-code pilot wedge).
    # Additive and self-contained — its own CREATE TABLE IF NOT EXISTS + indexes,
    # no shared column edits — so like agent_tokens above it does not need
    # reordering relative to any other block in this function. previous_version_id
    # and approval_id are plain nullable self/foreign references (SQLite does not
    # enforce FK constraints by default here, matching agent_tokens.rotated_from);
    # a missing referenced row is a read-time concern, not a write-time one.
    # tags is a JSON-encoded object (vertical-specific tags such as matter/client
    # come from the chosen template pack, never a hardcoded column).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_products (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          source_path TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          mime_type TEXT,
          producing_agent TEXT NOT NULL,
          producing_model TEXT,
          template_pack TEXT NOT NULL,
          tags TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'submitted'
            CHECK (status IN ('submitted', 'approved', 'rejected', 'released', 'tampered')),
          previous_version_id INTEGER REFERENCES work_products(id) ON DELETE SET NULL,
          approval_id INTEGER REFERENCES approvals(id) ON DELETE SET NULL,
          released_path TEXT,
          released_sha256 TEXT,
          reject_reason TEXT,
          submitted_by TEXT NOT NULL,
          resolved_by TEXT,
          resolved_at TEXT,
          released_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_products_status ON work_products(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_products_content_sha256 "
        "ON work_products(content_sha256)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_products_template_pack ON work_products(template_pack)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_products_approval_id ON work_products(approval_id)"
    )

    # Create the FTS-update trigger scoped to title/content (or replace an older
    # unscoped `AFTER UPDATE` trigger, exactly once). This stops the hot-path
    # access-stat bumps in memory_search/memory_get from re-indexing FTS on reads.
    trig = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memory_au'"
    ).fetchone()
    if trig is None or "UPDATE OF" not in (trig["sql"] or ""):
        conn.execute("DROP TRIGGER IF EXISTS memory_au")
        conn.executescript(_MEMORY_AU_TRIGGER)

    # org_id nullable tenant boundary (task #1232): deliberately the LAST thing
    # _migrate() does. Every CHECK-constraint table-rebuild above (tasks lane,
    # tasks status, approvals status, delegations attempts) copies a HARDCODED
    # column list that predates org_id and does not know about it — see
    # _guard_schema_version's docstring for exactly this hazard. Running these
    # ALTER TABLE ADD COLUMN calls only after every rebuild block has already
    # run means a rebuild can never see (and silently drop) a column that did
    # not exist yet when it ran; org_id is filled in fresh afterwards, on
    # whatever shape each table is left in. Additive-only, so this is the one
    # safe place: a future migration must keep adding new columns down here,
    # after this block, for the same reason.
    _add_org_id_columns(conn)
    # work_products.org_id (task #1834): its one-time backfill reads
    # event_log.org_id, which the line above adds on a pre-tenant database.
    _add_work_product_org_id(conn)
    # agent_sessions.org_id (task #1995): its one-time backfill reads
    # tasks.org_id, added by _add_org_id_columns above.
    _add_agent_session_org_id(conn)

    # agent_tokens.org_id (task #1343): token lifecycle rows are tenant-scoped
    # like the managed secrets they describe, so one tenant's rotation can never
    # pick up another tenant's row for the same agent label. Nullable, stamped
    # only under an enforced tenant boundary; single-tenant rows stay NULL.
    # Kept after _add_org_id_columns for the same rebuild-safety reason.
    # SCHEMA_VERSION 34.
    token_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()}
    if "org_id" not in token_cols:
        conn.execute("ALTER TABLE agent_tokens ADD COLUMN org_id TEXT")
    _rebuild_agent_tokens_without_global_hash_unique(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tokens_org_id ON agent_tokens(org_id)")
    # Duplicate-value detection per tenant (NULL org = the single-tenant partition).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tokens_org_hash "
        "ON agent_tokens(COALESCE(org_id, ''), token_hash)"
    )

    # agent_tokens.agent_kind (task #2453): the kind `unified-agent agent add`
    # gives a label, on the row that authenticates it. Additive nullable column
    # owned by agent_roster, after the rebuild above (which copies live columns);
    # not a SCHEMA_VERSION bump, same as the module-owned ensure_schema calls.
    from . import agent_roster

    agent_roster.ensure_schema(conn)

    # Durable OAuth store (task #1321, Epic J2): registered clients and
    # hash-keyed refresh tokens, so a hub restart logs no device out. Two new
    # self-contained tables (no column edits), so like the org_id block above it
    # cannot be dropped by a table rebuild. SCHEMA_VERSION 33.
    from . import oauth_store

    oauth_store.ensure_schema(conn)

    # Routine definitions and fire receipts are self-contained tables. A fire
    # and its task are committed together by the routine scheduler.
    table_schemas.routines(conn)

    # Goals and task comments (tasks #1349, #1423): self-contained tables.
    # goal ancestry is not a column on tasks, so this does not touch the
    # task-table rebuilds above. SCHEMA_VERSION 37.
    table_schemas.goals(conn)
    # goals.org_id (task #1999): its one-time backfill reads event_log.org_id,
    # added by _add_org_id_columns above.
    _add_goal_org_id(conn)
    # plans.org_id (task #2014): additive, after _add_org_id_columns.
    _add_plan_org_id(conn)
    # plans.revision and goals.revision (task #2480): optimistic concurrency.
    _add_revision_counters(conn)
    # agent_sessions.operator_ended_by (task #2480, #2502): durable operator-end marker.
    _add_operator_end_marker(conn)
    # node_dispatch.launch_key (task #2501): one queue item per Control Center launch.
    _add_node_dispatch_launch_key(conn)
    table_schemas.task_comments(conn)

    # Mission autonomy schema migrates at initialization, never while a
    # request evaluates or begins a governed action (#1544). Legacy JSON
    # grants move by the autonomy_modes.legacy_grants post-init hook.
    table_schemas.autonomy_modes(conn)

    # Operating profiles (task #1732): sprints and per-run settings tables,
    # self-contained, no edit to an existing table.
    from . import operating_policy

    operating_policy.migrate(conn)

    # Ranked task search (task #1419): task_fts is a second fts5 table
    # alongside memory_fts, same rowid-mirroring pattern, self-contained
    # (own virtual table + triggers, no tasks-table column edit). Rows that
    # predate it are indexed by the task_search.task_fts post-init hook.
    table_schemas.task_search(conn)

    # Skills registry (task #1353, Parity 6): org-wide skill versions and
    # their agent/profile assignments. Two self-contained tables, like the
    # goals/task_comments block above — safe to add here for the same
    # rebuild-safety reason.
    table_schemas.skills_registry(conn)

    # Project -> source-repository bindings (task #1425): one self-contained
    # table, no column edits to any existing table.
    table_schemas.project_bindings(conn)

    # node_dispatch.session_id (task #1328, Epic I2): the ACP session id a node
    # backend negotiated, written by post_receipt, so a dispatch joins to
    # agent_sessions.external_session_id. NULL for native backends and any row
    # predating the column. SCHEMA_VERSION 36.
    nd_cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_dispatch)").fetchall()}
    if nd_cols and "session_id" not in nd_cols:
        conn.execute("ALTER TABLE node_dispatch ADD COLUMN session_id TEXT")
    # #1392: additive profile provenance on the existing queue, distinct from
    # its authenticated device owner. Never backfill a guessed profile.
    for column in ("hermes_profile", "profile_agent", "permission_mode", "settings_source"):
        if nd_cols and column not in nd_cols:
            conn.execute(f"ALTER TABLE node_dispatch ADD COLUMN {column} TEXT")

    # Embedding model id (UA_EMBED_MODEL). Nullable: existing blobs are not
    # rewritten or backfilled. SCHEMA_VERSION 38.
    for table in ("memory_embeddings", "workspace_symbol_embeddings"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and "model" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN model TEXT")

    # tasks.current_run_id / current_run_agent (task #1526): additive cache of
    # the task's open self-pickup run, kept current by run_records.py. Placed
    # here, after both tasks_new rebuilds above, so an ancient DB that still
    # takes one of those rebuild paths gets these columns added to the *final*
    # tasks table rather than silently dropped by a column list that predates
    # them (the evidence_ref carry-through above is the same lesson).
    # SCHEMA_VERSION 39.
    run_cache_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "current_run_id" not in run_cache_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_run_id TEXT")
    if "current_run_agent" not in run_cache_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_run_agent TEXT")
    # Runs every init_db(), not just inside the ALTER branch: a fresh DB's
    # CREATE TABLE already has the column (see _SCHEMA) but never got the index.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_current_run_agent ON tasks(current_run_agent)"
    )
    # Tasks claimed before these columns existed get their cache filled by the
    # run_records.open_run_cache post-init hook (task #1526 follow-up).

    # Hub-observed provider usage ledger (task #1526): a self-contained table,
    # no column edits to any existing table — same rebuild-safety reasoning as
    # the goals/task_comments/skills_registry/project_bindings calls above.
    from . import usage_ledger

    usage_ledger.ensure_schema(conn)

    # Per-recipient mailbox index over the agent.message.* event log (task
    # #1859): one self-contained derived table, no column edits. Backfilled
    # from existing events on the call that creates it; not a SCHEMA_VERSION
    # bump, same as the module-owned ensure_schema tables above.
    from .messaging import index as message_index

    message_index.ensure_schema(conn)

    # Machine-readable record of the head a review read was performed at (task
    # #2259): one self-contained table, no column edits, so not a
    # SCHEMA_VERSION bump. The release gate reads it to refuse a head whose
    # red-tier delta was never read; the verdict itself stays human prose in
    # task notes, this is only the head binding.
    from . import review_reads

    review_reads.ensure_schema(conn)
    from . import approval_receipts  # durable decision receipts (task #2394)

    approval_receipts.ensure_schema(conn)
    from . import work_proposals

    work_proposals.ensure_schema(conn)
    from . import review_holds  # operator release/keep of review holds (task #2514)

    review_holds.ensure_schema(conn)
    from .lifecycle import handoff as session_handoff  # task #2397: self-contained tables

    session_handoff.ensure_schema(conn)

    # Operator follow-ups into live ACP node sessions (task #1740): one
    # self-contained table, no column edits; not a SCHEMA_VERSION bump.
    table_schemas.node_followups(conn)

    # Per-task stage, resume capsule and graded checks (task #2398): two
    # self-contained tables, no column edits; not a SCHEMA_VERSION bump.
    table_schemas.task_workflow(conn)
    # Independent-first design panels (task #2399): two self-contained
    # tables, no column edits; not a SCHEMA_VERSION bump.
    table_schemas.design_panels(conn)
    # Friction reports and fix-claim verdicts (task #2399): same shape.
    table_schemas.friction(conn)

    # Resume bindings for ua-node items (task #1741): one self-contained
    # table keyed by item id, no column edit to node_dispatch.
    table_schemas.node_dispatch_resume(conn)

    # Durable Slack approval-card deliveries (task #2303): one self-contained
    # table, no column edits; not a SCHEMA_VERSION bump.
    table_schemas.approval_slack_outbox(conn)

    # Planned checkout realpath for ua-node items (task #1770): one
    # self-contained table keyed by item id, no column edit to node_dispatch.
    table_schemas.node_dispatch_cwd(conn)

    # Waiting ua-node items (task #2110): queued for a node that was not live,
    # with a deadline. One self-contained table keyed by item id.
    table_schemas.node_dispatch_wait(conn)

    # Logical ua-node claim operations (task #2136): a lost claim response is
    # reconciled through one durable disposition per operation. One
    # self-contained table, no column edit to node_dispatch.
    table_schemas.node_claim_ops(conn)

    # Control Center launch keys bound to their dispatch run (task #2501): one
    # self-contained table, no column edit to dispatch_runs.
    table_schemas.dispatch_launch_keys(conn)

    # Session-scoped hub MCP credentials for node-launched ACP sessions (task
    # #1744): one self-contained table keyed by the credential's SHA-256, no
    # column edit to an existing table; not a SCHEMA_VERSION bump.
    table_schemas.session_credentials(conn)

    # Heartbeat usage for open self-pickup runs (task #1681, re-land of PR
    # #866): one row per run, self-contained, no edit to an existing table.
    table_schemas.run_usage_heartbeats(conn)

    # First-mate supervisor inbox cursors (task #1918): one row per consumer,
    # self-contained, no edit to an existing table.
    table_schemas.supervisor_cursors(conn)

    # One-shot monitors and declared waits (task #1749): one row per task,
    # self-contained, no edit to the tasks table; not a SCHEMA_VERSION bump.
    table_schemas.task_liveness(conn)

    # Completion return (task #1751): task origins and the delivery outbox.
    # Two self-contained tables plus a delete trigger on tasks; no column
    # edit to an existing table, so not a SCHEMA_VERSION bump.
    table_schemas.completion_return(conn)

    # Idle-session wake state (task #1873): one self-contained table keyed by
    # session id, no column edit to agent_sessions; not a SCHEMA_VERSION bump.
    table_schemas.session_wake(conn)

    # Device acceptance state for ua-nodes (task #2112): one self-contained
    # table keyed by node name, no column edit to nodes; not a SCHEMA_VERSION bump.
    table_schemas.node_acceptance(conn)

    # Memory entities and typed links (task #2096): two self-contained tables
    # plus org-fence triggers, no column edit to memory; not a SCHEMA_VERSION
    # bump. Older notes are backfilled by a post-init hook.
    table_schemas.memory_graph(conn)

    # Embedding chunks for long notes (task #2182): one self-contained table
    # plus org-fence triggers, no column edit to memory; not a SCHEMA_VERSION
    # bump. The vec0 mirror is created with memory_vec. Older notes are
    # backfilled by a post-init hook.
    table_schemas.memory_chunks(conn)

    # Prior note versions (task #2097): one self-contained table plus a
    # capture trigger on memory content/tags and a delete trigger; no column
    # edit to memory and no backfill, so not a SCHEMA_VERSION bump. Placed
    # after every memory migration above, so none of them is recorded.
    table_schemas.memory_versions(conn)

    # event_log append-only triggers (task #1908): one self-contained flag
    # table plus BEFORE UPDATE/DELETE triggers; no column edit to event_log.
    # Not a SCHEMA_VERSION bump, same as the ensure_schema calls above.
    table_schemas.event_log_append_only(conn)

    # tasks.cancel_requested (task #1525): additive, same placement reasoning
    # as current_run_id/current_run_agent just above — after any tasks_new
    # rebuild path, so an ancient DB gets it on the *final* tasks table.
    # SCHEMA_VERSION 40.
    cancel_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "cancel_requested" not in cancel_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")

    # Stored next-move ownership (task #2388): additive, same placement
    # reasoning as cancel_requested just above. SCHEMA_VERSION 43.
    table_schemas.task_ownership(conn)

    # `attempts` is decremented on release, so it cannot fence an executor's
    # delegation claim. This generation only increments and is never reset.
    # SCHEMA_VERSION 41.
    delegation_cols = {r["name"] for r in conn.execute("PRAGMA table_info(delegations)")}
    if "claim_generation" not in delegation_cols:
        conn.execute(
            "ALTER TABLE delegations ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0"
        )


def _rebuild_agent_tokens_without_global_hash_unique(conn: sqlite3.Connection) -> None:
    """Drop the table-wide UNIQUE on agent_tokens.token_hash, exactly once (#1343).

    SQLite cannot drop a column constraint in place, so a DB created before
    #1343 gets the standard copy-and-rename rebuild. Uniqueness moves to the
    per-tenant index created by the caller, so two tenants may record the same
    value while a duplicate inside one tenant is still refused. Column list is
    read from the live table, so no column is dropped by the copy.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_tokens'"
    ).fetchone()
    if row is None or "token_hash TEXT NOT NULL UNIQUE" not in (row["sql"] or ""):
        return
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()]
    # Every occurrence, including the self-referencing rotated_from FK, so the
    # RENAME below rewrites that reference back to agent_tokens (same idiom as
    # the tasks_new rebuild above). FKs are off for the swap: dropping the old
    # table must not fire ON DELETE SET NULL into the copied rotation chain.
    new_sql = row["sql"].replace("token_hash TEXT NOT NULL UNIQUE", "token_hash TEXT NOT NULL")
    new_sql = new_sql.replace("agent_tokens", "agent_tokens__rebuild")
    col_list = ", ".join(cols)
    # AUTOINCREMENT's high-water mark lives in sqlite_sequence and would be
    # reset to the highest *surviving* id by the copy; carry it over so a
    # retired token id is never issued again (audit refs stay unambiguous).
    seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'agent_tokens'").fetchone()
    high_water = int(seq_row["seq"]) if seq_row is not None else 0
    # One explicit transaction: a crash or error anywhere between the copy and
    # the rename rolls back to the original table, never to a dropped one that
    # the next init_db would silently re-create empty. PRAGMA foreign_keys can
    # only change outside a transaction, hence its placement around BEGIN/COMMIT.
    try:
        conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS agent_tokens__rebuild;
            {new_sql};
            INSERT INTO agent_tokens__rebuild ({col_list}) SELECT {col_list} FROM agent_tokens;
            DROP TABLE agent_tokens;
            ALTER TABLE agent_tokens__rebuild RENAME TO agent_tokens;
            CREATE INDEX IF NOT EXISTS idx_agent_tokens_label_status
              ON agent_tokens(agent_label, status);
            CREATE INDEX IF NOT EXISTS idx_agent_tokens_issued_at ON agent_tokens(issued_at);
            UPDATE sqlite_sequence SET seq = MAX(seq, {high_water}) WHERE name = 'agent_tokens';
            INSERT INTO sqlite_sequence (name, seq)
              SELECT 'agent_tokens', {high_water}
              WHERE {high_water} > 0
                AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'agent_tokens');
            COMMIT;
            """
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _add_org_id_columns(conn: sqlite3.Connection) -> None:
    """Add the nullable ``org_id`` tenant column to the five platform tables
    (task #1232), idempotently.

    Every column is plain ``TEXT``, nullable, no default, no CHECK — nothing
    ever *requires* it to be set. It is stamped at write time only when
    ``edition.tenant_org_id()`` returns non-``None`` (``UA_EDITION=enterprise``
    AND an org is configured); everywhere else, including today's private
    hub, every row keeps ``org_id IS NULL`` exactly as before this migration.
    No read path filters on it. See ``downgrade_org_id_columns`` for the
    tested reversal.
    """
    for table in ("tasks", "memory", "event_log", "approvals", "delegations"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "org_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN org_id TEXT")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_org_id ON {table}(org_id)")


def _add_work_product_org_id(conn: sqlite3.Connection) -> None:
    """Tenant column for work products (task #1834), idempotently.

    Nullable and stamped at insert like the other tenant columns. Rows that
    predate it take the tenant recorded on their own ``work_product.submitted``
    event, once; a row with no such event stays NULL (untenanted). No
    SCHEMA_VERSION bump: the column is additive and this runs on every start.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_products)").fetchall()}
    if "org_id" in cols:
        return
    conn.execute("ALTER TABLE work_products ADD COLUMN org_id TEXT")
    conn.execute(
        "UPDATE work_products SET org_id=(SELECT e.org_id FROM event_log e "
        "WHERE e.channel='work_product.submitted' "
        "AND json_extract(e.payload, '$.work_product_id')=work_products.id "
        "ORDER BY e.ROWID LIMIT 1)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_products_org_id ON work_products(org_id)")


def _add_agent_session_org_id(conn: sqlite3.Connection) -> None:
    """Tenant column for agent sessions (task #1995), idempotently.

    Nullable and stamped at insert from ``edition.tenant_org_id()``, like the
    other tenant columns. Rows that predate it take the ``org_id`` of the task
    they were opened against, once, when the column is added; a task-less row,
    or one whose task has no tenant, stays NULL (untenanted). No
    SCHEMA_VERSION bump: the column is additive and this runs on every start.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()}
    if "org_id" not in cols:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN org_id TEXT")
        conn.execute(
            "UPDATE agent_sessions SET org_id=(SELECT t.org_id FROM tasks t "
            "WHERE t.id=agent_sessions.task_id) WHERE task_id IS NOT NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_org_id ON agent_sessions(org_id)")


def _add_node_retired_columns(conn: sqlite3.Connection) -> None:
    """Retire marker for ``nodes`` (task #2181), idempotently.

    ``retired_at`` non-NULL marks a node an operator retired (a renamed or
    decommissioned device). The row is kept, never deleted; ``retired_by`` and
    ``retired_reason`` mirror the ``agent.node`` ledger event.
    ``heartbeat_floor`` (task #2196) takes the stored ``last_heartbeat`` on
    retire and un-retire: a beat at or before it predates that change and
    does not make the node live, so routing waits for a fresh registration
    or heartbeat while the stored beat stays as history. All nullable, so
    existing rows stay active. No SCHEMA_VERSION bump: additive, and this
    runs on every start.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for name in ("retired_at", "retired_by", "retired_reason", "heartbeat_floor"):
        if name not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} TEXT")


def _add_operator_end_marker(conn: sqlite3.Connection) -> None:
    """``agent_sessions.operator_ended_by`` (tasks #2480, #2502), idempotently.

    Set once by the operator's session end and never cleared: ``end_session``,
    the reaper and owner ends rewrite ``status`` and ``exit_status`` but not this
    column, and ``touch_session`` and ``register_session`` refuse to reopen a row
    that carries it. Additive and nullable: no SCHEMA_VERSION bump.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()}
    if cols and "operator_ended_by" not in cols:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN operator_ended_by TEXT")


def _add_node_dispatch_launch_key(conn: sqlite3.Connection) -> None:
    """``node_dispatch.launch_key`` and its unique index (task #2501), idempotently.

    A Control Center launch with an ``Idempotency-Key`` passes a key derived from
    tenant, verified identity and client key down to ``node_queue.enqueue``, which
    stores it on the item it creates. The partial unique index makes a second item
    for the same launch impossible, whatever the request path or a hub restart did,
    and lets a repeated launch find the item the first one created. Additive and
    nullable (every other enqueue leaves it NULL): no SCHEMA_VERSION bump.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_dispatch)").fetchall()}
    if not cols:
        return
    if "launch_key" not in cols:
        conn.execute("ALTER TABLE node_dispatch ADD COLUMN launch_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_node_dispatch_launch_key "
        "ON node_dispatch(launch_key) WHERE launch_key IS NOT NULL"
    )


def _add_revision_counters(conn: sqlite3.Connection) -> None:
    """Strictly increasing ``revision`` on plans and goals (task #2480), idempotently.

    The Control Center's plan and goal edits take ``expected_revision``; a
    timestamp has one-second precision, so two writes in the same second would
    look like one revision. An ``AFTER UPDATE`` trigger advances the counter on
    every update of the row that does not set it itself, so every writer (plan
    step writes touch ``plans.updated_at``) moves it without code changes, inside
    the writer's own transaction. Additive with a default of 0: no SCHEMA_VERSION
    bump, and this runs on every start.
    """
    for table in ("plans", "goals"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            continue
        if "revision" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_revision_bump AFTER UPDATE ON {table} "
            "FOR EACH ROW WHEN NEW.revision = OLD.revision BEGIN "
            f"UPDATE {table} SET revision = OLD.revision + 1 WHERE id = NEW.id; END"
        )


def _add_goal_org_id(conn: sqlite3.Connection) -> None:
    """Tenant column for goals (task #1999), idempotently.

    Nullable and stamped at insert from ``edition.tenant_org_id()``, like the
    other tenant columns. Rows that predate it take the tenant recorded on
    their own ``goal.created`` event, once, when the column is added; a goal
    with no such event stays NULL (untenanted). No SCHEMA_VERSION bump: the
    column is additive and this runs on every start.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(goals)").fetchall()}
    if "org_id" not in cols:
        conn.execute("ALTER TABLE goals ADD COLUMN org_id TEXT")
        event_cols = {r["name"] for r in conn.execute("PRAGMA table_info(event_log)").fetchall()}
        if "org_id" in event_cols:
            conn.execute(
                "UPDATE goals SET org_id=(SELECT e.org_id FROM event_log e "
                "WHERE e.channel='goal.created' "
                "AND json_extract(e.payload, '$.goal_id')=goals.id "
                "ORDER BY e.ROWID LIMIT 1)"
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_org_id ON goals(org_id)")


def _add_plan_org_id(conn: sqlite3.Connection) -> None:
    """Tenant column for plans (task #2014), idempotently, the goals pattern.

    Nullable and stamped at insert from ``edition.tenant_org_id()``. Plans
    record no tenant anywhere else, so rows that predate the column stay NULL
    (untenanted). Checkpoints carry no column: they belong to their plan. No
    SCHEMA_VERSION bump: the column is additive and this runs on every start.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "org_id" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN org_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_org_id ON plans(org_id)")


def downgrade_org_id_columns(conn: sqlite3.Connection) -> None:
    """Reverse ``_add_org_id_columns`` (task #1232): the down-path for the
    org_id migration.

    This repo's migration machinery (``_migrate``, above) is additive-only —
    idempotent ``ALTER TABLE ADD COLUMN`` calls with no companion "down"
    concept anywhere in ``storage.py``. There is no existing down-migration
    framework to plug into, so this reversal is implemented as its own
    idempotent, directly-callable function (the repo's idiom for a rollback
    script — see ``scripts/gate_e_rollback_rehearsal.py`` for the analogous
    pattern used for data, not schema, rollback) rather than invented as a
    parallel migration system. It is exercised end-to-end by
    ``tests/test_storage_migrations.py::test_org_id_migration_is_reversible``,
    which proves the prior schema (no ``org_id`` column, same indexes) is
    restored, not merely asserted in prose.

    Relies on SQLite's native ``ALTER TABLE ... DROP COLUMN`` (supported since
    SQLite 3.35.0 for a plain column with no CHECK/generated/indexed-in-a-
    partial-index complication, which org_id is not). Safe to call multiple
    times: a missing column or index is a no-op, not an error.

    The memory graph's org-fence triggers (task #2096) read ``memory.org_id``,
    so they are dropped first; ``init_db`` recreates them.
    """
    table_schemas.drop_memory_graph_fence(conn)
    for table in ("tasks", "memory", "event_log", "approvals", "delegations"):
        conn.execute(f"DROP INDEX IF EXISTS idx_{table}_org_id")
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "org_id" in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN org_id")


#: Tables that gained ``idx_<table>_org_id`` in task #2326 (#1433 slice 2): the
#: eighteen ``tenant_tables.ORG_INDEX_PENDING`` listed. Each index is declared
#: next to its table's CREATE statement.
ORG_ID_PARTITION_INDEX_TABLES = (
    "approval_slack_outbox",
    "autonomy_mission_mode",
    "autonomy_mode_revisions",
    "autonomy_pending_actions",
    "autonomy_standing_grants",
    "completion_deliveries",
    "connector_oauth_connections",
    "connector_oauth_pending",
    "harness_config_bundles",
    "harness_config_proposals",
    "memory_chunks",
    "message_index",
    "org_proposals",
    "routines",
    "session_credentials",
    "task_holds",
    "task_origins",
    "workflow_instances",
)


def downgrade_org_id_indexes(conn: sqlite3.Connection) -> None:
    """Drop the partition indexes task #2326 added; the down-path for that slice.

    Older code never names them, so a rollback does not need this; it exists
    so the change can be taken back out exactly. Safe to call repeatedly, and
    the next ``init_db`` recreates them. The same slice's key-constraint
    rebuild has no in-place reverse: the stricter schema keeps every column
    and older code opens it unchanged, and the rows it deleted (orphan
    comments, NULL keys) are in the backup file it recorded in ``meta``
    (``constraint_migration.MARKER_KEY``).
    """
    for table in ORG_ID_PARTITION_INDEX_TABLES:
        conn.execute(f"DROP INDEX IF EXISTS idx_{table}_org_id")


class SchemaVersionError(RuntimeError):
    """The DB's stored schema_version is ahead of what this code understands."""


def _guard_schema_version(conn: sqlite3.Connection) -> None:
    """Refuse to run against a DB newer than this code understands (task #373
    Tier 3, 4.6).

    _migrate() is additive-only for plain ALTER TABLE ADD COLUMN calls, which
    are low-risk even run against a newer DB — an unrecognized existing
    column is simply left alone. The real hazard is the CHECK-constraint
    table-rebuild blocks (tasks/approvals/delegations/autonomous_cycles):
    each rebuilds its table from a HARDCODED column list. Older code's rebuild
    block does not know about a column a newer version of this code added —
    if that rebuild's guard condition ever fired against a newer DB, it would
    silently drop that column during the copy, not error. Checking the
    version BEFORE any schema statement runs (this is the first thing
    init_db() does) means a mismatch is refused outright rather than
    discovered after partial mutation.

    Must not raise on a brand-new DB (no `meta` table yet, or no
    `schema_version` row) — that is the normal first-run case, not a
    version mismatch.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return  # meta table doesn't exist yet: brand-new DB, nothing to guard
    if row is None:
        return  # meta exists but no schema_version row yet: pre-#(this task) DB
    try:
        stored_version = int(row["value"])
    except (TypeError, ValueError):
        return  # unparseable stored value — not this guard's job to diagnose
    if stored_version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema_version={stored_version} is newer than this code "
            f"understands (SCHEMA_VERSION={SCHEMA_VERSION}). Refusing to start: "
            f"running older code against a newer DB risks _migrate()'s CHECK-"
            f"constraint table-rebuild blocks silently dropping columns they "
            f"don't recognize. Update this checkout (git pull / switch branch) "
            f"before starting, or point UA_HOME at a different database."
        )


def get_schema_version(conn: sqlite3.Connection | None = None) -> int:
    """Return the schema_version stored in the database, or 0 if not set."""
    close_conn = False
    if conn is None:
        conn = connect()
        close_conn = True
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            return 0
        return int(row["value"])
    except (sqlite3.OperationalError, TypeError, ValueError):
        return 0
    finally:
        if close_conn:
            conn.close()


def verify_schema_integrity(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Verify database schema version, tables, indexes, and SQLite consistency."""
    close_conn = False
    if conn is None:
        conn = connect()
        close_conn = True
    try:
        stored_version = get_schema_version(conn)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }

        quick_check = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
        fk_check = conn.execute("PRAGMA foreign_key_check").fetchall()

        core_tables = {"tasks", "memory", "event_log", "approvals", "delegations", "meta", "agents"}
        missing_tables = sorted(core_tables - tables)

        # Indexes on core tables are part of the schema contract; a dropped or
        # wrongly-recreated index passes quick_check but degrades the hub.
        core_indexes = {
            "idx_tasks_status_priority",
            "idx_tasks_parent",
            "idx_memory_updated",
            "idx_event_log_channel",
            "idx_event_log_created",
            "idx_event_log_channel_created",
            "idx_event_log_episode",
        }
        missing_core_indexes = sorted(core_indexes - indexes)

        valid = (
            stored_version <= SCHEMA_VERSION
            and len(missing_tables) == 0
            and len(missing_core_indexes) == 0
            and quick_check == ["ok"]
            and len(fk_check) == 0
        )

        return {
            "valid": valid,
            "status": "ok" if valid else "drift",
            "stored_schema_version": stored_version,
            "target_schema_version": SCHEMA_VERSION,
            "missing_core_tables": missing_tables,
            "missing_core_indexes": missing_core_indexes,
            "quick_check": quick_check,
            "foreign_key_errors_count": len(fk_check),
            "tables_count": len(tables),
            "indexes_count": len(indexes),
        }
    finally:
        if close_conn:
            conn.close()


def init_db() -> None:
    _guard_disposable_synthetic_store()
    config.ensure_dirs()
    path = config.db_path()  # the store this call opens, recorded on success
    conn = connect(scoped=False)  # migrations see the stored org_id values
    try:
        _guard_schema_version(conn)
        conn.executescript(_SCHEMA)
        conn.executescript(_AGENT_EVENTS_SCHEMA)  # additive block, see above
        conn.executescript(_AGENT_PERF_SCHEMA)  # additive block, see above
        conn.executescript(_ANOMALY_NOTIFY_SCHEMA)  # additive block, see above
        _migrate_anomaly_notifications(conn)
        conn.executescript(_DELEGATION_SCHEMA)  # additive block (task 62), see above
        conn.executescript(_EXECUTION_GRAPH_SCHEMA)  # additive block (task 256)
        conn.executescript(_TASK_HOLD_SCHEMA)  # additive subtree holds (task #1747)
        _migrate_task_holds(conn)
        conn.executescript(_AUTONOMOUS_CYCLE_SCHEMA)  # additive block (task 259)
        conn.executescript(_DISPATCH_RUNS_SCHEMA)  # additive block (task #548)
        conn.executescript(_DB_SIZE_SCHEMA)  # additive block (task 64 phase D), see above
        conn.executescript(_EVENT_LOG_SCHEMA)  # additive block (task #157), see above
        _migrate_node_turn_index(conn)
        conn.executescript(_ZULIP_SCHEMA)  # additive cursor state for the Zulip worker
        # Additive cursor state for the task-space (Discord/Telegram) worker
        # (task #770); schema block lives in table_schemas.
        conn.executescript(table_schemas.TS_CURSORS)
        conn.executescript(_SLACK_CHAT_SCHEMA)  # additive Slack live-chat claim/continuity
        conn.executescript(_EXECUTOR_EVIDENCE_SCHEMA)  # additive evidence (task #458)
        conn.executescript(_AGENT_REGISTRY_SCHEMA)  # additive block (webui M1), see above
        conn.executescript(_HOST_REPORTS_SCHEMA)  # additive block (task #798), see above
        conn.executescript(_NODE_SCHEMA)  # additive ua-node queue (task #1327)
        _add_node_retired_columns(conn)  # operator node retire (task #2181)
        conn.executescript(_DEVICE_PROOF_REPLAY_SCHEMA)  # single-use device proofs (#2132)
        conn.executescript(
            _HARNESS_CONFIG_SCHEMA
        )  # additive hub-served harness config (task #1330)
        conn.executescript(
            _ORG_PROPOSALS_SCHEMA
        )  # additive self-organization proposals (task #1431)
        conn.executescript(_CONNECTOR_OAUTH_SCHEMA)  # additive outbound connector OAuth (J5)
        conn.executescript(_IDEMPOTENCY_SCHEMA)  # additive block (task #429), see above
        conn.executescript(_HORIZON2_SCHEMA)  # additive connector governance (task #664)
        table_schemas.governed_servers(conn)  # nullable org ownership; no legacy adoption (#2513)
        conn.executescript(_GRAPH_CANVAS_SCHEMA)  # additive planning canvas (task #807)
        conn.executescript(_ELICITATION_CHALLENGE_SCHEMA)  # additive (task #832)
        conn.executescript(_FRICTION_NUDGE_SCHEMA)  # additive (task #914), see above
        conn.executescript(_RECALL_CURATION_SCHEMA)  # additive (task #915), see above
        conn.executescript(_MEMORY_INDEX_DIRTY_SCHEMA)  # additive (task #1914), see above
        conn.executescript(table_schemas.WORKFLOW_ROLES)  # role frontiers (#1737)
        dirty_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_index_dirty)")}
        if "generation" not in dirty_cols:
            conn.execute(
                "ALTER TABLE memory_index_dirty ADD COLUMN generation TEXT NOT NULL DEFAULT ''"
            )
        _ensure_memory_vec_table(conn)  # optional, sqlite-vec extension — see above
        _ensure_memory_chunk_vec_table(conn)  # optional mirror of memory_chunks
        _migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        edition.ensure_store_tenant(conn, initialized=True)
        conn.commit()
        # The #1433 key constraints on a store whose tables predate them
        # (task #2326): one shot, marker-gated, backup first, all or nothing.
        # Last, on a committed schema, in its own transaction.
        from . import constraint_migration

        constraint_migration.run(conn)
        # A store an operator partitioned stays whole across schema growth
        # (task #1433). No marker, no work: this never starts a backfill.
        from . import org_partition_migration

        org_partition_migration.reconcile(conn)
    finally:
        conn.close()

    run_post_init_hooks()
    # Only a fully initialized store counts: a foreground hook (migration)
    # that raised above must be retried by the next init_db_once() call.
    _initialized_db_paths.add(str(path))


# Stores this process has run init_db() against (the hub's start-up init
# included). Request-time callers use init_db_once() instead of init_db(), so a
# store that is already initialized is not re-scripted, re-migrated and
# re-hooked inside a write transaction on every request, while a fresh data
# home (a CLI's first write, a test's tmp UA_HOME) still bootstraps.
_initialized_db_paths: set[str] = set()
_init_db_once_lock = threading.Lock()


def forget_initialized(path: Path | str | None = None) -> None:
    """Make the next init_db_once() re-initialize ``path`` (default: the
    current store), e.g. after its contents were replaced by a restore."""
    target = str(path if path is not None else config.db_path())
    with _init_db_once_lock:
        _initialized_db_paths.discard(target)


def init_db_once() -> None:
    """Run init_db() unless this process already initialized this store."""
    path = config.db_path()
    with _init_db_once_lock:
        if str(path) in _initialized_db_paths and path.is_file():
            return
        init_db()


# --- post-init hooks ------------------------------------------------------------
#
# init_db() above is schema only: every table, column, index and trigger. Work
# that reads or rewrites rows, or warms a cache, belongs to a module above core
# (autonomy grants, task_fts, the open-run cache, embedding backfills, the vault
# state snapshot), so storage cannot name it. Those modules' work is registered
# here instead, by ``post_init.register_default_hooks()``, and every init_db()
# in a process that registered them runs them, in order, once the schema is
# committed. A process that registered none (a bare script that imports only
# storage) gets the schema alone; the hub registers them before its first
# init_db, so its start-up still migrates and backfills.


@dataclass(frozen=True)
class PostInitHook:
    """One unit of post-init work.

    A foreground hook is called as ``fn(conn)`` on a shared connection and
    committed before the next one runs; an exception rolls it back and
    propagates out of init_db, as a failed migration always has. A background
    hook is called as ``fn()`` on its own daemon thread, where an exception
    ends only that thread (embedding a cold cache can download a model, and
    the MCP host is mid-handshake while init_db runs, so blocking would stall
    registration).
    """

    name: str
    fn: Callable[..., Any]
    order: int
    background: bool = False


_post_init_hooks: dict[str, PostInitHook] = {}
_post_init_lock = threading.Lock()


def register_post_init_hook(
    name: str, fn: Callable[..., Any], *, order: int, background: bool = False
) -> None:
    """Register (or replace, by ``name``) work for every later init_db()."""
    with _post_init_lock:
        _post_init_hooks[name] = PostInitHook(name, fn, order, background)


def unregister_post_init_hook(name: str) -> None:
    with _post_init_lock:
        _post_init_hooks.pop(name, None)


def post_init_hooks() -> list[PostInitHook]:
    """Registered hooks in run order: ascending ``order``, then name."""
    with _post_init_lock:
        hooks = list(_post_init_hooks.values())
    return sorted(hooks, key=lambda hook: (hook.order, hook.name))


def run_post_init_hooks() -> None:
    """Run the registered hooks: foreground ones in order, then start the rest.

    Called at the end of init_db(). The background threads are tracked so
    join_init_background_threads can wait for them: they resolve config (vault
    path, DB path) from the environment at run time, so a straggler that
    outlives an environment change writes into the NEW environment's vault/DB
    (the test_sync debounce-test flake).
    """
    global _init_background_threads
    synthetic = os.environ.get("UA_SYNTHETIC_WORKFLOW") == "1"
    if synthetic:
        require_disposable_synthetic_home()
    hooks = post_init_hooks()
    if synthetic:
        # Keep local migrations/caches, but a copied fixture may contain
        # pending messages. Never start inherited drains or model workers.
        hooks = [hook for hook in hooks if not hook.background]
    foreground = [hook for hook in hooks if not hook.background]
    if foreground:
        conn = connect()
        try:
            for hook in foreground:
                try:
                    hook.fn(conn)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        finally:
            conn.close()
    # Single-flight per (hook, database). init_db() is also called at request
    # time (system_doctor's connector check calls it once per connector), and
    # each call used to start a fresh thread per background hook. Every
    # backfill thread holds its own connection while it waits its turn on the
    # serialized embedding model, so a polled caller stacked up threads and
    # memory.db handles until the hub ran out of file descriptors. A hook
    # still running for this database is reused, not duplicated; a hook for a
    # different database (a test's fresh UA_HOME) still starts.
    db = str(config.db_path())
    threads: list[threading.Thread] = []
    with _background_hook_lock:
        # Only live threads matter for single-flight; drop finished ones so a
        # process that visits many data homes does not keep one per hook each.
        for stale in [k for k, t in _background_hook_threads.items() if not t.is_alive()]:
            del _background_hook_threads[stale]
        for hook in hooks:
            if not hook.background:
                continue
            key = (hook.name, db)
            running = _background_hook_threads.get(key)
            if running is None or not running.is_alive():
                running = threading.Thread(
                    target=hook.fn, name=f"ua-post-init-{hook.name}", daemon=True
                )
                _background_hook_threads[key] = running
                running.start()
            threads.append(running)
    _init_background_threads = threads


_init_background_threads: list[threading.Thread] = []
_background_hook_threads: dict[tuple[str, str], threading.Thread] = {}
_background_hook_lock = threading.Lock()


def join_init_background_threads(timeout: float = 10.0) -> None:
    """Wait for the background work spawned by the last init_db call.

    Hermeticity hook for tests (and orderly shutdown): without it, a startup
    thread delayed past the caller's next config/env change fires against the
    new configuration — e.g. dumping a state snapshot into a test vault
    created after init_db returned.

    Joins every live post-init hook, not only the last init_db call's: a hook
    still running for an earlier database resolves its paths at run time, so
    it must finish before the caller changes the environment (PR #1001)."""
    with _background_hook_lock:
        pending = {*_init_background_threads, *_background_hook_threads.values()}
    for t in pending:
        t.join(timeout=timeout)


# Index notes are generated aggregates, never embedded, and must be excluded
# from any "is this note embedded?" question. The startup backfill
# (memory_semantic.backfill_memory_embeddings) uses the identical predicate —
# the two disagreeing would make embedding_coverage() report a permanent
# backlog the backfill never clears.
_EMBEDDABLE_NOTES = "id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%'"


def embedding_coverage() -> dict:
    """Count embeddable notes, how many lack an embedding, and mirror drift.

    Pure SQL, so this answers "is semantic search degraded on this machine?"
    without loading (or downloading) the model. Used by system_doctor.

    The `memory_vec` half is not redundant with the `memory_embeddings` half.
    When sqlite-vec is available, `_semantic_similarity_rows` reads the vec0
    mirror and returns **nothing at all** if it is empty — it falls back to the
    per-row UDF scan only on `OperationalError`, which an empty-but-present
    table does not raise. `upsert_memory_vec` swallows every exception by
    design, so the mirror can silently fail to keep up while
    `memory_embeddings` (the source of truth) stays complete. Counting only the
    source of truth would report full coverage on a machine whose semantic
    search returns zero results for every query.

    When the mirror is present and source coverage is complete, run one KNN
    query using an existing source vector. This exercises the actual vec0
    search path without loading the model; a malformed vector or broken index
    is reported as unusable rather than as a healthy-but-accelerated store.

    `mirror_available` is False when sqlite-vec is not loaded on this
    connection: there is no mirror to drift, and the UDF path serves searches.
    """
    conn = connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM memory WHERE {_EMBEDDABLE_NOTES}").fetchone()[0]
        missing = conn.execute(
            f"SELECT COUNT(*) FROM memory WHERE {_EMBEDDABLE_NOTES} "
            "AND id NOT IN (SELECT note_id FROM memory_embeddings)"
        ).fetchone()[0]
        # NULL model marks a pre-migration blob. Those rows stay comparable
        # only while UA_EMBED_MODEL is still the default. A nondefault model
        # excludes them from cosine search, so they are a coverage gap even
        # though a memory_embeddings row exists. Default-model hosts keep
        # counting them as embedded.
        legacy_excluded = 0
        if config.embed_model() != config.DEFAULT_EMBED_MODEL:
            legacy_excluded = conn.execute(
                "SELECT COUNT(*) FROM memory m "
                "JOIN memory_embeddings me ON me.note_id = m.id "
                "WHERE m.id NOT LIKE 'masterindex%' AND m.id NOT LIKE 'tagindex_%' "
                "AND me.model IS NULL"
            ).fetchone()[0]
        mirror_available = True
        mirror_missing = 0
        mirror_usable = True
        mirror_error = None
        # A vec0 table can exist in sqlite_master while the extension is
        # unavailable on this connection. Count first to distinguish that
        # normal fallback from an index that is present but unusable.
        try:
            conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()
        except sqlite3.OperationalError:
            mirror_available = False
        else:
            try:
                # Only 384-float blobs can live in memory_vec. A wider or
                # narrower model is served by the per-row scan, so its absence
                # from the mirror is not a coverage gap and must not be probed.
                mirror_missing = conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings me JOIN memory m ON m.id = me.note_id "
                    "WHERE m.rowid NOT IN (SELECT rowid FROM memory_vec) "
                    "AND LENGTH(me.embedding) = ?",
                    (MEMORY_VEC_DIM * 4,),
                ).fetchone()[0]
                if total and not missing and mirror_missing == 0:
                    probe = conn.execute(
                        "SELECT embedding FROM memory_embeddings me JOIN memory m ON m.id = me.note_id "
                        f"WHERE {_EMBEDDABLE_NOTES.replace('id', 'm.id')} "
                        "AND LENGTH(me.embedding) = ? LIMIT 1",
                        (MEMORY_VEC_DIM * 4,),
                    ).fetchone()
                    if probe is not None:
                        result = conn.execute(
                            "SELECT rowid FROM memory_vec WHERE embedding MATCH ? AND k = 1",
                            (probe["embedding"],),
                        ).fetchone()
                        if result is None:
                            mirror_usable = False
                            mirror_error = "KNN probe returned no rows"
            except sqlite3.Error as exc:
                mirror_usable = False
                mirror_error = str(exc)
    finally:
        conn.close()
    return {
        "total": total,
        "embedded": total - missing,
        "missing": missing,
        "legacy_excluded": legacy_excluded,
        "mirror_available": mirror_available,
        "mirror_missing": mirror_missing,
        "mirror_usable": mirror_usable,
        "mirror_error": mirror_error,
    }


def canonical_embedding_model(model: str | None) -> str:
    """Model id for deciding whether two stored vectors may be cosine-compared.

    NULL and blank mark a pre-migration blob from fastembed's default. That
    alias is the legacy rule: those rows compare with the default id and with
    each other, and with no other model. A configured ``UA_EMBED_MODEL`` does
    not rewrite the alias.
    """
    if model is None or not str(model).strip():
        return config.DEFAULT_EMBED_MODEL
    return str(model).strip()


def compatible_embedding_model_clause(column: str) -> tuple[str, tuple[str, ...]]:
    """SQL predicate for vectors comparable with the current embedding model.

    A stored id must equal ``UA_EMBED_MODEL``. NULL marks a pre-migration blob
    produced by fastembed's default; those rows stay comparable only while the
    current model is still that default. Anything else is left in the table
    (not rewritten) and is omitted from cosine scans so a model change cannot
    score unrelated coordinates. Keyword search still reaches those notes.
    """
    current = config.embed_model()
    if current == config.DEFAULT_EMBED_MODEL:
        return f"({column} IS NULL OR {column} = ?)", (current,)
    return f"{column} = ?", (current,)


def stored_embedding_model_ids() -> list[str]:
    """Distinct model ids on memory embeddings.

    Symbol vectors are a separate index. Another workspace's symbol rows must
    not fail the memory ``semantic_search`` check; those ids come from
    ``stored_symbol_embedding_model_ids``. NULL rows are omitted. This does
    not load the embedding model and does not rewrite any blob.
    """
    conn = connect()
    try:
        try:
            rows = conn.execute(
                "SELECT DISTINCT model FROM memory_embeddings "
                "WHERE model IS NOT NULL AND model != ''"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return sorted(row["model"] for row in rows if row["model"])
    finally:
        conn.close()


def stored_symbol_embedding_model_ids(workspace: str) -> list[str]:
    """Distinct non-null symbol-embedding model ids for one workspace.

    Scoped by ``workspace_symbols.workspace``. NULL rows are omitted here;
    ``symbol_embedding_coverage`` counts them. This does not load the model
    and does not rewrite any blob.
    """
    return list(symbol_embedding_coverage(workspace)["models"])


def symbol_embedding_coverage(workspace: str) -> dict:
    """Symbol-vector counts for one workspace.

    ``total`` is every stored vector. ``legacy`` is rows whose model is NULL
    only: pre-migration blobs from the default model. A blank or whitespace
    model is ``blank``, not legacy. Search treats blank as a different id
    from NULL, so those rows are excluded from compatible coverage.
    ``models`` is the distinct non-empty ids. Another workspace's rows are
    not included. An empty result is no vectors, which is different from a
    workspace whose vectors are all legacy or all blank.
    """
    empty = {"total": 0, "legacy": 0, "blank": 0, "models": []}
    conn = connect()
    try:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN e.model IS NULL THEN 1 ELSE 0 END), 0) "
                "AS legacy, "
                "COALESCE(SUM(CASE WHEN e.model IS NOT NULL AND "
                "TRIM(e.model, char(9) || char(10) || char(11) || char(12) || char(13) || ' ') = '' "
                "THEN 1 ELSE 0 END), 0) AS blank "
                "FROM workspace_symbol_embeddings e "
                "JOIN workspace_symbols s ON s.id = e.symbol_id "
                "WHERE s.workspace = ?",
                (workspace,),
            ).fetchone()
            labeled = conn.execute(
                "SELECT DISTINCT e.model FROM workspace_symbol_embeddings e "
                "JOIN workspace_symbols s ON s.id = e.symbol_id "
                "WHERE s.workspace = ? AND e.model IS NOT NULL AND "
                "TRIM(e.model, char(9) || char(10) || char(11) || char(12) || char(13) || ' ') != ''",
                (workspace,),
            ).fetchall()
        except sqlite3.OperationalError:
            return empty
        return {
            "total": int(row["total"] or 0),
            "legacy": int(row["legacy"] or 0),
            "blank": int(row["blank"] or 0),
            "models": sorted(item["model"] for item in labeled if item["model"]),
        }
    finally:
        conn.close()


def get_search_cache(provider: str, query: str, max_results: int) -> dict | None:
    """Retrieve search results from cache if present."""
    import json
    import re

    norm_query = re.sub(r"\s+", " ", query.strip().lower())
    conn = connect()
    try:
        row = conn.execute(
            "SELECT results, fetched_at FROM web_search_cache WHERE provider=? AND query=? AND max_results=?",
            (provider, norm_query, max_results),
        ).fetchone()
        if row:
            return {"results": json.loads(row["results"]), "fetched_at": row["fetched_at"]}
        return None
    except Exception:
        return None
    finally:
        conn.close()


def save_search_cache(provider: str, query: str, max_results: int, results: list[dict]) -> None:
    """Save search results to cache."""
    import json
    import re

    norm_query = re.sub(r"\s+", " ", query.strip().lower())
    conn = connect()
    try:
        ts = now()
        conn.execute(
            "INSERT OR REPLACE INTO web_search_cache (provider, query, max_results, results, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (provider, norm_query, max_results, json.dumps(results), ts),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def prune_search_cache(ttl_minutes: float) -> None:
    """Prune expired search cache entries."""
    from datetime import UTC, datetime, timedelta

    conn = connect()
    try:
        threshold = (datetime.now(UTC) - timedelta(minutes=ttl_minutes)).isoformat()
        conn.execute("DELETE FROM web_search_cache WHERE fetched_at < ?", (threshold,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def ingest_ledger_get(path: str) -> dict | None:
    """Fetch a vault_ingest_ledger row by vault-relative path, or None if absent."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT path, content_hash, ingested_at, agent, pages, reason "
            "FROM vault_ingest_ledger WHERE path=?",
            (path,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ingest_ledger_mark(
    path: str,
    content_hash: str,
    agent: str | None,
    pages: str,
    reason: str | None = None,
) -> None:
    """Upsert the vault_ingest_ledger row for `path` with the given content hash.

    `pages` is a JSON-encoded list of wiki pages the ingest fed; `ingested_at`
    is stamped as now (UTC). `reason` is an optional human note stored with the
    actor attribution (task #1157).
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO vault_ingest_ledger "
            "(path, content_hash, ingested_at, agent, pages, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, content_hash, now(), agent, pages, reason),
        )
        conn.commit()
    finally:
        conn.close()


# --- graph planning canvas (task #807) -----------------------------------------
# Verbatim Excalidraw scene persistence, one row per board id. Accessors live
# here (not in tools/graph_canvas.py) so the webui route and the MCP tool share
# one storage path — the UI and the agents must never disagree about what is on
# the canvas.


def get_graph_canvas_scene(board_id: str) -> dict[str, Any] | None:
    """Return the stored scene row for `board_id`, or None if never written."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT board_id, scene_json, updated_by, updated_at "
            "FROM graph_canvas_scenes WHERE board_id=?",
            (board_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "board_id": row["board_id"],
        "scene": json.loads(row["scene_json"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def put_graph_canvas_scene(board_id: str, scene: Any, updated_by: str) -> dict[str, Any]:
    """Upsert the scene for `board_id`, attributed to `updated_by`.

    The caller is responsible for validating `scene` is a JSON-compatible dict;
    this function serializes it deterministically (sort_keys) so byte-equality
    comparisons in tests are stable.
    """
    scene_json = json.dumps(scene, sort_keys=True)
    updated_at = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO graph_canvas_scenes "
            "(board_id, scene_json, updated_by, updated_at) VALUES (?, ?, ?, ?)",
            (board_id, scene_json, updated_by, updated_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "board_id": board_id,
        "scene": scene,
        "updated_by": updated_by,
        "updated_at": updated_at,
    }


# --- end graph planning canvas (task #807) ------------------------------------
