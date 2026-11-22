"""The hub's generated-note writers take the per-note lock vault_write takes (#2086).

consolidate, sleep_pass, evolve, harness_config, merge and the audit reanchor
write vault notes directly rather than through ``vault_write``. Each one must
queue on ``note_write_lock`` for the note it writes, so it cannot land between
another hub writer's lock check and write. Every contention test holds the lock
on the target note from the test thread, starts the writer in a worker, waits
until the worker signals it is about to queue on that note's lock, and only then
checks the worker is still blocked and the note untouched. No fixed sleep: a
writer that never takes the lock fails on the signal, however slow the host.
Nothing here depends on the volume's case sensitivity: each writer's target is
spelled exactly as the writer spells it.
"""

from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_agent import (
    config,
    consolidate,
    evolve,
    harness_config,
    llm_client,
    merge,
    note_lock,
    sleep_pass,
    storage,
)
from unified_agent.note_lock import note_write_lock
from unified_agent.tools.memory import memory_save

_STAMP = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
#: Upper bound on any wait for a signal; a healthy run never comes close.
_SIGNAL_TIMEOUT = 30


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _STAMP if tz is not None else _STAMP.replace(tzinfo=None)


@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "Logs"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    return root


def _start(fn, *args, **kwargs):
    ctx = contextvars.copy_context()
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = ctx.run(fn, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


def _contend(monkeypatch, target: Path, fn, *args, while_held=None, **kwargs):
    """Run ``fn`` against a held lock on ``target``; return its outcome once released.

    The lock-file hook sets ``queued`` just before the worker's ``flock`` on
    ``target``'s key, so the bounded join after it observes a worker that has
    reached the lock, not one that has not started yet.
    """
    key = note_lock._note_lock_name(target)
    real = note_lock._hold_note_lock_file
    queued = threading.Event()

    @contextlib.contextmanager
    def signalling(name):
        if name == key:
            queued.set()
        with real(name):
            yield

    before = target.read_bytes() if target.exists() else None
    with note_write_lock(target):
        monkeypatch.setattr(note_lock, "_hold_note_lock_file", signalling)
        worker, outcome = _start(fn, *args, **kwargs)
        reached = queued.wait(timeout=_SIGNAL_TIMEOUT)
        assert reached, f"{fn.__qualname__} never queued on {target.name}'s note lock"
        worker.join(timeout=0.05)
        assert worker.is_alive(), f"{fn.__qualname__} wrote {target.name} past a held lock"
        now = target.read_bytes() if target.exists() else None
        assert now == before, f"{target.name} changed while the lock was held"
        if while_held is not None:
            while_held()
    worker.join(timeout=_SIGNAL_TIMEOUT)
    assert not worker.is_alive(), f"{fn.__qualname__} never finished after the lock was released"
    return outcome


def _ok(outcome: dict):
    assert "error" not in outcome, outcome
    return outcome.get("result")


def test_consolidate_proposal_waits_for_the_note_lock(vault, monkeypatch):
    monkeypatch.setattr(consolidate, "datetime", _FixedDatetime)
    target = vault / "00 - Inbox" / "Memory-Consolidation-Proposal-20261014-120000.md"
    result = _ok(_contend(monkeypatch, target, consolidate._write_artifact, "# proposal\n"))
    assert Path(result) == target.resolve()
    assert target.read_text(encoding="utf-8") == "# proposal\n"


def test_sleep_pass_report_waits_for_the_note_lock(vault, monkeypatch):
    monkeypatch.setattr(sleep_pass, "datetime", _FixedDatetime)
    target = vault / "00 - Inbox" / "Sleep-Pass-20261014-120000.md"
    result = _ok(_contend(monkeypatch, target, sleep_pass._write_artifact, "# sleep\n"))
    assert Path(result) == target.resolve()
    assert target.read_text(encoding="utf-8") == "# sleep\n"


def test_evolve_proposal_waits_for_the_note_lock(vault, monkeypatch):
    target = vault / evolve.PROPOSAL_RELPATH
    result = _ok(_contend(monkeypatch, target, evolve._write_artifact, "# skills\n"))
    assert Path(result) == target.resolve()
    assert target.read_text(encoding="utf-8") == "# skills\n"


def test_evolve_fingerprint_is_checked_under_the_note_lock(vault, monkeypatch):
    # A writer holding the lock files the same proposal; the waiting pass must
    # see it and report it as existing, not rewrite it.
    target = vault / evolve.PROPOSAL_RELPATH
    filed = "---\nfingerprint: abc123\n---\nFILED\n"
    outcome = _contend(
        monkeypatch,
        target,
        evolve._write_artifact,
        "# skills\n",
        fingerprint="abc123",
        while_held=lambda: target.write_text(filed, encoding="utf-8"),
    )
    result = _ok(outcome)
    assert result is evolve._UNCHANGED
    assert isinstance(result, evolve.ArtifactUnchanged)
    assert target.read_text(encoding="utf-8") == filed


def test_harness_config_inbox_proposal_waits_for_the_note_lock(vault, monkeypatch):
    target = vault / "00 - Inbox" / "harness-config" / "desk-claude-v1.md"
    target.parent.mkdir(parents=True)
    outcome = _contend(
        monkeypatch,
        target,
        harness_config._write_inbox_proposal,
        node="desk",
        surface="claude",
        version="v1",
        path="~/.claude.json",
        diff="+x\n",
        proposal_id="hc-1",
    )
    assert _ok(outcome) == "00 - Inbox/harness-config/desk-claude-v1.md"
    assert "hc-1" in target.read_text(encoding="utf-8")


def test_merge_failure_report_waits_for_the_note_lock(vault, monkeypatch):
    target = vault / "00 - Inbox" / "Merge-Failed-Task-42.md"
    outcome = _contend(
        monkeypatch, target, merge.write_failed_report, 42, "tests failed", "1 failed", ""
    )
    assert _ok(outcome) == target.resolve()
    assert "Task 42" in target.read_text(encoding="utf-8")


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()


def test_reanchor_waits_for_the_note_lock(vault, ledger_db, monkeypatch):
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True)
    result = _ok(_contend(monkeypatch, target, reanchor_chain_head, day="2026-10-14"))
    assert result["anchor_action"] == "created"
    assert result["head_hash"] in target.read_text(encoding="utf-8")


def test_reanchor_existing_note_is_read_under_the_note_lock(vault, ledger_db, monkeypatch):
    # A snapshot filed while the reanchor waited is an external witness: the
    # waiting writer must append to it, never replace it with a fresh note.
    from unified_agent import events
    from unified_agent.audit.reanchor import reanchor_chain_head

    events.record_event("agent.task.updated", payload={"task_id": 7})
    target = vault / "Logs" / "audit-ledger-heads" / "2026-10-14.md"
    target.parent.mkdir(parents=True)
    # The witness shape this writer files; a foreign locked note is refused
    # instead (#2091, tests/test_generated_writer_locked.py).
    witness = (
        b"---\ntype: audit-ledger-head\nlocked: true\ndate: 2026-10-14\n---\n\n"
        b"# Audit ledger chain head 2026-10-14\n\n- head_hash: earlier\n"
    )
    outcome = _contend(
        monkeypatch,
        target,
        reanchor_chain_head,
        day="2026-10-14",
        while_held=lambda: target.write_bytes(witness),
    )
    result = _ok(outcome)
    assert result["anchor_action"] == "appended"
    text = target.read_bytes()
    assert text.startswith(witness)
    assert result["head_hash"].encode() in text


# --- evolve and sleep_pass filing semantics around the lock -----------------


def _two_related_trusted_notes() -> None:
    for content, title in (
        ("Always call task_claim before editing unified-agent product files.", "Claim first"),
        ("Never edit unified-agent product files until task_claim succeeds.", "No edit unclaimed"),
    ):
        memory_save(
            content,
            title=title,
            tags=["fact", "project:unified-agent"],
            confidence=0.9,
            author="Desktop_Agent4",
            salience=0.8,
        )


def test_evolve_unreadable_existing_proposal_propagates(vault, monkeypatch):
    # The fingerprint read moved under the lock; an unreadable note must still
    # fail the pass, as the unlocked check did, not divert to a report file
    # that is then reported as the vault path.
    _two_related_trusted_notes()

    def unreadable(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(evolve, "_existing_fingerprint", unreadable)
    with pytest.raises(PermissionError):
        evolve.run_evolve_pass(write=True)
    assert not (vault / evolve.PROPOSAL_RELPATH).exists()
    assert not (config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name).exists()


def test_evolve_fallback_reports_its_own_path(vault, monkeypatch):
    _two_related_trusted_notes()
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Logs")  # the inbox is not writable
    result = evolve.run_evolve_pass(write=True)
    fallback = config.data_home() / "reports" / Path(evolve.PROPOSAL_RELPATH).name
    assert result["written"] is True
    assert result["path"] == str(fallback)
    assert fallback.is_file()
    assert not (vault / evolve.PROPOSAL_RELPATH).exists()


def _record_txn_state_at_lock(monkeypatch) -> list[str]:
    """Record the sleep pass's read connection state each time a note lock is taken."""
    conns: list[sqlite3.Connection] = []
    states: list[str] = []
    real_connect = sleep_pass._connect_ro
    real_hold = note_lock._hold_note_lock_file

    def connect():
        conn = real_connect()
        conns.append(conn)
        return conn

    def state(conn: sqlite3.Connection) -> str:
        try:
            return "open" if conn.in_transaction else "idle"
        except sqlite3.ProgrammingError:
            return "closed"

    @contextlib.contextmanager
    def recording(name):
        states.extend(state(c) for c in conns)
        with real_hold(name):
            yield

    monkeypatch.setattr(sleep_pass, "_connect_ro", connect)
    monkeypatch.setattr(note_lock, "_hold_note_lock_file", recording)
    monkeypatch.setattr(llm_client, "is_on_battery", lambda: False)
    return states


def test_sleep_pass_files_evolve_proposal_after_the_read_transaction(vault, monkeypatch):
    _two_related_trusted_notes()
    states = _record_txn_state_at_lock(monkeypatch)
    monkeypatch.setattr(
        llm_client,
        "complete",
        lambda prompt, system=None, **kw: {
            "text": "VERDICT: UNRELATED\nREASON: test",
            "provider": "fake",
            "model": "fake",
        },
    )
    result = sleep_pass.run_sleep_pass(write=True)
    assert result["evolve"]["written"] is True, result["evolve"]
    assert result["evolve"]["path"] == evolve.PROPOSAL_RELPATH
    assert (vault / evolve.PROPOSAL_RELPATH).is_file()
    assert states, "no note lock was taken"
    assert set(states) == {"closed"}, states


def test_sleep_pass_files_deferred_report_after_the_read_transaction(vault, monkeypatch):
    _two_related_trusted_notes()
    states = _record_txn_state_at_lock(monkeypatch)
    monkeypatch.setenv("UA_SLEEP_PASS_MAX_CANDIDATES", "1")
    result = sleep_pass.run_sleep_pass(write=True)
    assert result["status"] == "deferred-preflight"
    assert result["artifact_path"] and Path(result["artifact_path"]).is_file()
    assert states, "no note lock was taken"
    assert set(states) == {"closed"}, states


@pytest.mark.parametrize(
    ("module", "name", "phase"),
    [
        (consolidate, "_load_candidates", "evolve"),
        (sleep_pass, "_find_pruning_candidates", "pruning"),
    ],
)
def test_sleep_pass_files_computed_proposal_when_a_later_read_fails(
    vault, monkeypatch, module, name, phase
):
    # The proposal is computed before candidate loading and pruning. A failure
    # there must still file it (once, after the snapshot closes) and must
    # surface the read's own error, not a filing one.
    _two_related_trusted_notes()
    states = _record_txn_state_at_lock(monkeypatch)
    boom = RuntimeError("injected read failure")

    def failing(*args, **kwargs):
        raise boom

    monkeypatch.setattr(module, name, failing)
    filed: list[dict] = []
    real_file = evolve.file_proposal

    def counting(result):
        out = real_file(result)
        filed.append(out)
        return out

    monkeypatch.setattr(evolve, "file_proposal", counting)
    with pytest.raises(RuntimeError) as raised:
        sleep_pass.run_sleep_pass(write=True)
    assert raised.value is boom
    assert raised.value.sleep_pass_telemetry["phase"] == phase
    assert len(filed) == 1
    assert filed[0]["written"] is True
    assert (vault / evolve.PROPOSAL_RELPATH).is_file()
    assert set(states) == {"closed"}, states
