"""Post-init hooks: the row migrations and backfills that follow ``init_db()``.

``storage.init_db()`` is schema only (core). The work below reads or rewrites
rows, or warms a cache, and each piece belongs to a module above core, so it is
registered with ``storage.register_post_init_hook`` here and run by every later
``storage.init_db()`` in the same process (task #1720).

Run order (foreground hooks first, each on the init connection and committed
before the next; then the background hooks, each on a daemon thread):

====  ==================================  ==========  ===========================
order name                                kind        work
====  ==================================  ==========  ===========================
10    autonomy_modes.legacy_grants        foreground  JSON grants -> grant table
20    task_search.task_fts                foreground  index tasks missing from FTS
30    run_records.open_run_cache          foreground  fill tasks.current_run_*
40    tools.memory.graph_backfill         foreground  memory entities and links
110   memory_semantic.embeddings          background  embed notes, mirror memory_vec
115   memory_chunks.backfill              background  chunk long notes, mirror them
120   task_search.embeddings              background  embed tasks in batches
130   tools.indexer.symbol_embeddings     background  embed workspace symbols
140   sync.startup_snapshot               background  vault state snapshot
150   completion_return.startup_drain     background  send pending completion returns
160   approval_slack.startup_drain        background  resume unfinished approval cards
====  ==================================  ==========  ===========================

This is the order the same work ran in when ``init_db`` did it inline
(the completion-return drain, task #1751, the memory graph backfill,
task #2096, and the approval-card drain, task #2303, came later).

Entry points that call :func:`init_db` here (the hub server, CLIs, workers)
get the full start-up. Core modules that may only import storage call
``storage.init_db()`` and get whatever their process registered.

Each hook resolves its target when it runs, not when it is registered, so a
test that patches e.g. ``task_search.backfill_all_task_embeddings`` before
``init_db()`` still takes effect, as it did when storage looked the name up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from . import storage


def _legacy_grants(conn: sqlite3.Connection) -> None:
    from . import autonomy_modes

    autonomy_modes.migrate_legacy_grants(conn)


def _task_fts(conn: sqlite3.Connection) -> None:
    from . import task_search

    task_search.backfill_task_fts(conn)


def _open_run_cache(conn: sqlite3.Connection) -> None:
    from . import run_records

    run_records.backfill_open_run_cache(conn)


def _memory_graph_backfill(conn: sqlite3.Connection) -> None:
    from .tools import memory

    memory.backfill_memory_graph(conn)


def _memory_embeddings() -> None:
    from . import memory_semantic

    memory_semantic.backfill_memory_embeddings()


def _memory_chunks() -> None:
    from . import memory_chunks

    memory_chunks.backfill_memory_chunks()


def _task_embeddings() -> None:
    from . import task_search

    task_search.backfill_all_task_embeddings()


def _symbol_embeddings() -> None:
    try:
        from .tools.indexer import _backfill_symbol_embeddings
    except (ImportError, AttributeError):
        return
    _backfill_symbol_embeddings()


def _startup_snapshot() -> None:
    from . import sync

    sync.sync_on_startup()


def _completion_return_drain() -> None:
    from . import completion_return

    completion_return.startup_drain()


def _approval_slack_drain() -> None:
    from . import approval_slack

    approval_slack.startup_drain()


DEFAULT_HOOKS: tuple[tuple[str, Callable[..., Any], int, bool], ...] = (
    ("autonomy_modes.legacy_grants", _legacy_grants, 10, False),
    ("task_search.task_fts", _task_fts, 20, False),
    ("run_records.open_run_cache", _open_run_cache, 30, False),
    ("tools.memory.graph_backfill", _memory_graph_backfill, 40, False),
    ("memory_semantic.embeddings", _memory_embeddings, 110, True),
    ("memory_chunks.backfill", _memory_chunks, 115, True),
    ("task_search.embeddings", _task_embeddings, 120, True),
    ("tools.indexer.symbol_embeddings", _symbol_embeddings, 130, True),
    ("sync.startup_snapshot", _startup_snapshot, 140, True),
    ("completion_return.startup_drain", _completion_return_drain, 150, True),
    ("approval_slack.startup_drain", _approval_slack_drain, 160, True),
)


def register_kernel_handlers() -> None:
    """Load the feature modules that install kernel handlers when imported (#2450).

    The kernel may not import its features, so each one registers itself with
    the kernel module that calls it: ``merge`` gives ``tools.git`` the merge
    orchestrator, and ``github_app_fastpath`` gives ``tools.approvals`` the
    ledger fast path. Every entry point that registers the default hooks gets
    both, as it did when the kernel imported them itself.
    """
    from . import github_app_fastpath, merge  # noqa: F401


def register_default_hooks() -> None:
    """Register the hub's post-init work and kernel handlers. Idempotent."""
    register_kernel_handlers()
    for name, fn, order, background in DEFAULT_HOOKS:
        storage.register_post_init_hook(name, fn, order=order, background=background)


def init_db() -> None:
    """``storage.init_db()`` with the hub's post-init hooks registered first.

    Looks ``storage.init_db`` up at call time, so a test that replaces it
    still sees the call.
    """
    register_default_hooks()
    storage.init_db()
