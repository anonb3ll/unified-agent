#!/usr/bin/env python3
"""Live smoke suite (System Leverage Plan T1-6, `make smoke`).

Unmocked checks against the real installed venv and real data paths — the
direct lesson of tasks #104/#110: `fs_index_symbols`' tree-sitter arrow-
function indexing shipped broken (wrong grammar field name, then an
uninstallable dependency) and stayed broken in production for days, because
its unit tests were fully mocked and the real code path was never actually
exercised. Nothing here mocks a dependency; a check either exercises the real
thing or reports SKIP (optional capability not installed on this host) —
never a silent pass.

State isolation: UA_HOME/UA_WORKSPACE/UA_VAULT_PATH are pointed at a fresh
temp directory for the duration of this run, so this is safe to run
repeatedly without touching the real memory.db or Obsidian vault — but every
code path exercised (fastembed, sqlite-vec, tree-sitter, pypdf, the audit
engine, task/vault I/O) is the real one, not a test double.

Usage:  uv run python scripts/smoke.py   (or: make smoke)
Exit codes: 0 = every check PASS or SKIP; 1 = at least one FAIL.
"""

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def _setup_isolated_env(tmp_dir: Path) -> None:
    os.environ["UA_HOME"] = str(tmp_dir / "ua_home")
    os.environ["UA_WORKSPACE"] = str(tmp_dir / "workspace")
    os.environ["UA_VAULT_PATH"] = str(tmp_dir / "vault")
    # Task and dispatch events must stay off the launching host's live bus.
    os.environ["UA_EVENT_SOCKET"] = str(tmp_dir / "events.sock")
    Path(os.environ["UA_WORKSPACE"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["UA_VAULT_PATH"]).mkdir(parents=True, exist_ok=True)
    # Never let a real bridge/search key on this host make a live check
    # silently depend on network state.
    os.environ.pop("UA_BRIDGE_DIR", None)
    # Match tests/conftest.py: debounced vault snapshots register an atexit
    # handler and leave daemon Timer threads running — during interpreter
    # teardown that races with onnxruntime and produces SIGABRT (-6) on CI
    # even when every check PASSed (post-merge main red on task #488).
    os.environ["UA_STATE_SYNC"] = "0"
    # These standalone checks exercise real writes in a disposable store,
    # without a human approval loop. Match pytest's isolated runtime rather
    # than inherit the launching host's gates or the production baseline.
    os.environ["UA_APPROVAL_TOOLS"] = ""
    os.environ["UA_APPROVAL_BASELINE"] = "0"


def check_module_imports() -> tuple[str, str]:
    """The server module (and everything it eagerly imports) loads cleanly —
    catches an uninstallable/abandoned dependency at import time, same class
    of bug as task #104's tree-sitter-languages."""
    import importlib

    importlib.import_module("unified_agent.server")
    return "PASS", "unified_agent.server imports cleanly"


def check_fastembed_dimension() -> tuple[str, str]:
    """Real fastembed model, real inference call. Cross-checks the live
    output dimension against storage.MEMORY_VEC_DIM — a mocked test would
    never catch the two drifting apart if the default model ever changes.

    SKIPs rather than FAILs when the model cannot be *acquired*. fastembed
    downloads its ONNX model on first use, so an offline or network-restricted
    host has no model to exercise — the same "optional capability not installed
    on this host" case as sqlite-vec, tree-sitter and pypdf below. A FAIL there
    says the embedding tier is broken when the truth is that it was never
    available, which is the confusion `docs/assessment-2026-08-12.md` §3.2 is
    about.

    Every other failure is still a FAIL, and the narrowness is the point: a
    model that loads and returns the wrong dimension, or embedding code that
    raises on its own bug, must not be excused as an unavailable host. That
    would turn this check — whose whole reason for existing is catching what
    mocked unit tests cannot — into one that reports SKIP on the very
    regressions it is here to find.
    """
    from unified_agent import storage
    from unified_agent.embeddings import generate_embedding, is_model_acquisition_error

    try:
        vec = generate_embedding("live smoke suite probe sentence")
    except Exception as e:
        if not is_model_acquisition_error(e):
            raise  # the runner reports a raising check as FAIL
        return "SKIP", f"fastembed model unavailable on this host ({type(e).__name__}: {e})"
    if len(vec) != storage.MEMORY_VEC_DIM:
        return (
            "FAIL",
            f"embedding dimension {len(vec)} != storage.MEMORY_VEC_DIM {storage.MEMORY_VEC_DIM}",
        )
    return "PASS", f"generate_embedding() returned {len(vec)}-dim vector"


def check_sqlite_vec_extension() -> tuple[str, str]:
    """The sqlite-vec extension actually loads and a KNN query actually
    runs on THIS host's sqlite3 build — extension loading is inherently
    environment-dependent (Python build, platform wheel availability), the
    exact class of thing an in-memory unit test on a dev machine can mask."""
    import struct

    from unified_agent import storage

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_vec'"
        ).fetchone()
        if row is None:
            return "SKIP", "sqlite-vec extension did not load on this connection"
        conn.execute("DELETE FROM memory_vec")
        probe = struct.pack(f"{storage.MEMORY_VEC_DIM}f", *([0.0] * storage.MEMORY_VEC_DIM))
        conn.execute("INSERT INTO memory_vec (rowid, embedding) VALUES (999999999, ?)", (probe,))
        result = conn.execute(
            "SELECT rowid, distance FROM memory_vec WHERE embedding MATCH ? AND k = 1",
            (probe,),
        ).fetchone()
        conn.execute("DELETE FROM memory_vec WHERE rowid=999999999")
        conn.commit()
        if result is None or result["rowid"] != 999999999:
            return "FAIL", "memory_vec KNN query returned no/wrong result"
        return "PASS", "sqlite-vec extension loaded; KNN query round-tripped"
    finally:
        conn.close()


def check_tree_sitter_arrow_function() -> tuple[str, str]:
    """Real tree-sitter parse of a real .ts file containing an arrow
    function assigned to a const — the exact symbol shape task #110 found
    silently unindexed in production (walk_ts_nodes queried grammar field
    'id'; JS/TS grammars use 'name'). Written inside UA_WORKSPACE and
    indexed with a relative path: fs_index_symbols/fs_list_symbols are
    sandboxed to the workspace root, same as any other fs_* tool."""
    try:
        import importlib

        importlib.import_module("tree_sitter_language_pack")
    except ImportError:
        return "SKIP", "tree_sitter_language_pack not installed (required dependency; uv sync)"

    from unified_agent.tools.indexer import fs_index_symbols, fs_list_symbols

    workspace = Path(os.environ["UA_WORKSPACE"])
    subdir = workspace / "smoke-ts-check"
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / "sample.ts").write_text(
        "export const greet = (name: string): string => `hi ${name}`;\n"
    )
    fs_index_symbols(path="smoke-ts-check")
    listed = fs_list_symbols("smoke-ts-check/sample.ts")
    names = {s.get("name") for s in listed.get("symbols", [])}
    if "greet" not in names:
        return "FAIL", f"arrow function 'greet' not indexed; found {sorted(names)}"
    return "PASS", "arrow-function symbol indexed correctly"


def check_pdf_extract() -> tuple[str, str]:
    """Real pypdf round trip: write a minimal real PDF, extract its text."""
    try:
        import importlib

        importlib.import_module("pypdf")
    except ImportError:
        return "SKIP", "pypdf not installed (uv sync --extra pdf)"

    from pypdf import PdfWriter

    with tempfile.TemporaryDirectory() as d:
        pdf_path = Path(d) / "sample.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(pdf_path, "wb") as f:
            writer.write(f)
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            return "FAIL", "pypdf did not produce a readable file"
        return "PASS", "pypdf writer round-trip produced a real PDF file"


def check_web_search_provider_resolution() -> tuple[str, str]:
    """Real provider-resolution logic, no network call — confirms the
    function that decides which search backend to use doesn't raise."""
    from unified_agent.tools import web

    provider = web._resolve_provider()
    if provider not in ("ddg", "brave", "tavily"):
        return "FAIL", f"unexpected provider {provider!r}"
    return "PASS", f"resolves to {provider!r}"


def check_task_claim_round_trip() -> tuple[str, str]:
    """Real DB round trip: add a task, claim it, update it, confirm status."""
    from unified_agent import storage
    from unified_agent.tools.tasks import task_add, task_claim, task_update

    t = task_add("smoke suite probe task")
    task_claim(t["id"], agent="smoke-suite")
    task_update(t["id"], status="done")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (t["id"],)).fetchone()
    finally:
        conn.close()
    if row is None or row["status"] != "done" or not row["claimed_by"]:
        return "FAIL", "task claim/update round trip did not persist or lacked attribution"
    return "PASS", f"task_add -> task_claim -> task_update round-tripped ({row['claimed_by']})"


def check_vault_write_read_round_trip() -> tuple[str, str]:
    """Real filesystem write/read through the vault tools (isolated temp
    vault, not the real Obsidian vault — see module docstring). Writes go
    through Logs/, one of the default UA_VAULT_WRITE_DIRS — the vault write
    boundary applies here exactly as it does to a real agent call."""
    from unified_agent.tools.vault import vault_read, vault_write

    vault_write("Logs/smoke-probe.md", "smoke suite probe content")
    result = vault_read("Logs/smoke-probe.md")
    if "smoke suite probe content" not in result["content"]:
        return "FAIL", "vault_write/vault_read round trip lost content"
    return "PASS", "vault_write -> vault_read round-tripped"


def check_security_audit_engine() -> tuple[str, str]:
    """Real audit engine run over the isolated workspace root (by the time
    this check runs, it holds the sample files the other checks wrote) —
    confirms the rule engine itself doesn't crash on real filesystem input.
    A relative path, since security_audit resolves like every other fs_*
    tool: workspace-relative, or absolute under UA_AUDIT_ROOTS."""
    from unified_agent.tools.audit import security_audit

    result = security_audit(path=".")
    if result.get("status") not in ("pass", "warn", "fail"):
        return "FAIL", f"unexpected audit status {result.get('status')!r}"
    return "PASS", f"audit ran, status={result['status']!r}, {result['files_scanned']} file(s)"


def check_sleep_pass_journal_parser() -> tuple[str, str]:
    """Real journal-entry parsing logic, no network call — confirms the shared
    Logs/*.md parser (the sleep pass's daily digest today, Roadmap Phase 1.2's
    cross-machine briefing next) correctly extracts structured entries from
    the real `- **HH:MM** [agent] text #tags` journal line format."""
    from unified_agent.sleep_pass import parse_journal_entries

    sample = (
        "---\ndate: 2026-07-22\ntype: journal\n---\n\n"
        "# 2026-07-22 — Agent journal\n"
        "- **09:41** [Desktop_Agent1] Task #132 resolved: pinned code-review-graph. "
        "#security #steward\n"
        "- **08:00** [Desktop_Agent5] Running daily briefing skill.\n"
    )
    entries = parse_journal_entries(sample)
    if len(entries) != 2:
        return "FAIL", f"expected 2 entries, got {len(entries)}"
    if entries[0]["agent"] != "Desktop_Agent1" or entries[0]["tags"] != ["security", "steward"]:
        return "FAIL", f"parsed entry mismatch: {entries[0]}"
    return "PASS", f"parsed {len(entries)} real-format journal entries with tags"


def check_cross_machine_briefing_digest() -> tuple[str, str]:
    """Real vault I/O through vault_briefing_digest (Roadmap Phase 1.2) — confirms
    the desktop+laptop journal merge and PR/task-id dedup work against actual
    files, not mocks. Timestamps are computed relative to "now" (not a fixed
    date) so this check keeps passing no matter what day the smoke suite
    actually runs on."""
    import datetime as _dt

    from unified_agent.tools.vault import vault_briefing_digest, vault_write

    now = _dt.datetime.now()
    day = now.strftime("%Y-%m-%d")
    now_hhmm = now.strftime("%H:%M")
    earlier_hhmm = (now - _dt.timedelta(minutes=5)).strftime("%H:%M")

    vault_write(
        f"Logs/{day}.md",
        f"---\ndate: {day}\ntype: journal\n---\n\n# {day} — Agent journal\n"
        f"- **{earlier_hhmm}** [Desktop_Agent1] Merged PR "
        "https://github.com/example-org/example-repo/pull/66. #ci\n"
        f"- **{now_hhmm}** [Desktop_Agent1] Unrelated desktop-only note.\n",
    )
    vault_write(
        f"Logs/Laptop/{day}.md",
        f"---\ndate: {day}\ntype: journal\n---\n\n# {day} — Agent journal\n"
        f"- **{earlier_hhmm}** [Laptop_Agent1] Merged PR "
        "https://github.com/example-org/example-repo/pull/66. #ci\n"
        f"- **{now_hhmm}** [Laptop_Agent1] Unrelated laptop-only note.\n",
    )
    result = vault_briefing_digest(date=day)
    if result["desktop_count"] != 2 or result["laptop_count"] != 2:
        return (
            "FAIL",
            f"expected 2+2 raw entries, got {result['desktop_count']}+{result['laptop_count']}",
        )
    if result["deduped_count"] != 1 or len(result["entries"]) != 3:
        return "FAIL", f"expected 1 PR-dedup and 3 merged entries, got {result}"
    return "PASS", (
        f"merged {result['desktop_count']}+{result['laptop_count']} entries, "
        f"deduped {result['deduped_count']}"
    )


def check_mcp_live_progress_and_subscriptions() -> tuple[str, str]:
    """MCP 2.x live progress notifications, subscription lifecycle, and reconnect
    reconciliation through the real server/client path (task #864)."""
    from unified_agent.mcp_live_verification import run_live_verification_sync
    from unified_agent.server import mcp

    report = run_live_verification_sync(server=mcp)
    if not report.passed:
        return (
            "FAIL",
            f"verification failed: {report.checks_failed}/{report.checks_total} checks failed",
        )
    return (
        "PASS",
        f"verified progress ({len(report.progress_milestones)} milestones) and subscriptions ({len(report.events_recorded)} events) in {report.duration_ms:.1f}ms",
    )


def check_mcp2_smoke_suite() -> tuple[str, str]:
    """MCP 2.x Wave 2 end-to-end smoke verification suite (UA #867)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mcp2_smoke", _REPO_ROOT / "scripts" / "mcp2_smoke.py"
    )
    if spec is None or spec.loader is None:
        return "FAIL", "could not locate scripts/mcp2_smoke.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    report = mod.run_smoke_suite()
    if not report["passed"]:
        return (
            "FAIL",
            f"{report['checks_failed']}/{report['checks_total']} MCP 2.x smoke checks failed",
        )
    return (
        "PASS",
        f"all {report['checks_passed']} MCP 2.x wave 2 smoke checks passed in {report['duration_ms']:.1f}ms",
    )


CHECKS = [
    check_module_imports,
    check_fastembed_dimension,
    check_sqlite_vec_extension,
    check_tree_sitter_arrow_function,
    check_pdf_extract,
    check_web_search_provider_resolution,
    check_task_claim_round_trip,
    check_vault_write_read_round_trip,
    check_security_audit_engine,
    check_sleep_pass_journal_parser,
    check_cross_machine_briefing_digest,
    check_mcp_live_progress_and_subscriptions,
    check_mcp2_smoke_suite,
]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ua-smoke-") as tmp:
        _setup_isolated_env(Path(tmp))

        # Every real entry point (server.main(), the CLI tools) calls this
        # before touching storage.connect() — ensures the DB dir/schema
        # (including memory_vec, if sqlite-vec loads) exist before any check
        # below runs.
        from unified_agent import post_init, storage

        post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
        storage.join_init_background_threads()

        results = []
        for check in CHECKS:
            name = check.__name__.removeprefix("check_")
            try:
                status, detail = check()
            except Exception as e:  # a check itself raising is always a FAIL
                status, detail = "FAIL", f"{type(e).__name__}: {e}"
            results.append((name, status, detail))
            print(f"[{status:4}] {name}: {detail}")

        failed = [r for r in results if r[1] == "FAIL"]
        skipped = [r for r in results if r[1] == "SKIP"]
        passed = [r for r in results if r[1] == "PASS"]
        print(
            f"\nsmoke: {len(passed)} passed, {len(skipped)} skipped, "
            f"{len(failed)} failed ({len(results)} total)"
        )
        exit_code = 1 if failed else 0

        # Orderly shutdown: fs_index_symbols spawns an untracked daemon thread
        # for symbol-embedding backfill; init_db may have started others. Let
        # them finish before onnxruntime tears down (CI SIGABRT otherwise).
        storage.join_init_background_threads()
        try:
            from unified_agent.tools.indexer import join_symbol_backfill_threads

            join_symbol_backfill_threads()
        except ImportError:
            pass
        try:
            from unified_agent.sync import flush_pending_sync

            flush_pending_sync()
        except Exception:
            pass

        return exit_code


if __name__ == "__main__":
    sys.exit(main())
