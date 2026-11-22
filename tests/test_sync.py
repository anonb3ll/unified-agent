import json

import pytest

from unified_agent import config
from unified_agent import sync as sync_mod
from unified_agent.sync import (
    flush_pending_sync,
    schedule_sync_to_vault,
    sync_state_to_vault,
)
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add


@pytest.fixture(autouse=True)
def state_sync_on(monkeypatch):
    """Opt this module back in: conftest defaults UA_STATE_SYNC off (#201).

    Every test here is *about* the sync layer, so it needs the switch on. Tests
    exercising the off-switch set it to "0" themselves in the body, which wins.
    """
    monkeypatch.setenv("UA_STATE_SYNC", "1")


def _make_vault(tmp_path, monkeypatch):
    """Create a temp vault with the allowed write dirs and patch config to use it."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Wiki").mkdir()
    (vault_dir / "00 - Inbox").mkdir()
    (vault_dir / "Logs").mkdir()
    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    monkeypatch.setattr(config, "vault_write_dirs", lambda: ["00 - Inbox", "Logs", "Wiki"])
    return vault_dir


def test_sync_state_to_vault(tmp_path, monkeypatch):
    # Set up temp vault root
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()

    # Create the allowed write directories under vault
    (vault_dir / "Wiki").mkdir()
    (vault_dir / "00 - Inbox").mkdir()
    (vault_dir / "Logs").mkdir()

    # Patch vault_root and vault_write_dirs to point to tmp_path
    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    monkeypatch.setattr(config, "vault_write_dirs", lambda: ["00 - Inbox", "Logs", "Wiki"])

    # Insert some data
    # 1. Add memory
    res_mem = memory_save(
        content="This is test content", title="Test Title", tags=["test-tag"], author="test-author"
    )

    # 2. Add task
    res_task = task_add(
        title="Test Task Title",
        notes="Test task notes\nclaimed-by: test-agent @ 2026-07-12T12:00:00Z\n",
        priority="high",
    )

    # Manually trigger sync (though memory_save and task_add already trigger it)
    sync_state_to_vault()

    # Check files exist
    mem_txt_path = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")
    tasks_txt_path = vault_dir / "Logs" / config.state_mirror_name("state_tasks", ".txt")
    mem_md_path = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".md")
    tasks_md_path = vault_dir / "Logs" / config.state_mirror_name("state_tasks", ".md")

    assert mem_txt_path.exists()
    assert tasks_txt_path.exists()
    assert mem_md_path.exists()
    assert tasks_md_path.exists()

    # Validate JSON contents
    memories = json.loads(mem_txt_path.read_text(encoding="utf-8"))
    tasks = json.loads(tasks_txt_path.read_text(encoding="utf-8"))

    # Verify memory
    assert len(memories) >= 1
    test_mem = next(m for m in memories if m["id"] == res_mem["id"])
    assert test_mem["title"] == "Test Title"
    assert test_mem["content"] == "This is test content"
    assert "test-tag" in test_mem["tags"]
    assert test_mem["author"] == "test-author"

    # Verify task
    assert len(tasks) >= 1
    test_task = next(t for t in tasks if t["id"] == res_task["id"])
    assert test_task["title"] == "Test Task Title"
    assert test_task["notes"] == "Test task notes\nclaimed-by: test-agent @ 2026-07-12T12:00:00Z\n"
    assert test_task["priority"] == "high"

    # Verify MD summary contents
    mem_md_content = mem_md_path.read_text(encoding="utf-8")
    assert "Test Title" in mem_md_content
    assert "This is test content" in mem_md_content

    tasks_md_content = tasks_md_path.read_text(encoding="utf-8")
    assert "Test Task Title" in tasks_md_content
    assert "test-agent" in tasks_md_content


def test_sync_no_vault_configured(monkeypatch):
    # Patch vault_root to return None
    monkeypatch.setattr(config, "vault_root", lambda: None)

    # Should complete without error
    sync_state_to_vault()


def test_sync_non_existent_vault(tmp_path, monkeypatch):
    # Patch vault_root to return a non-existent path
    non_existent = tmp_path / "does-not-exist"
    monkeypatch.setattr(config, "vault_root", lambda: non_existent)

    # Should complete without error
    sync_state_to_vault()


def test_sync_disabled_switch(tmp_path, monkeypatch):
    # Set up temp vault root
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Logs").mkdir()

    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    monkeypatch.setenv("UA_STATE_SYNC", "0")

    # This should be skipped because UA_STATE_SYNC=0
    sync_state_to_vault()

    mem_md_path = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".md")
    assert not mem_md_path.exists()


def test_sync_backticks_escaping(tmp_path, monkeypatch):
    # Set up temp vault root
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Logs").mkdir()

    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)

    # Add task with notes containing backticks
    task_add(
        title="Escaping Test",
        notes="Code block: `inline` and ```\nfence escape\n``` test",
        priority="low",
    )

    sync_state_to_vault()

    tasks_md_path = vault_dir / "Logs" / config.state_mirror_name("state_tasks", ".md")
    tasks_md_content = tasks_md_path.read_text(encoding="utf-8")

    # Assert that actual triple backticks inside codeblock in notes are escaped
    assert "\\`\\`\\`" in tasks_md_content
    # The literal ``` fence shouldn't be ended by raw triple backticks
    assert "```text" in tasks_md_content
    # But notes themselves should be escaped
    assert "inline" in tasks_md_content


def test_schedule_debounce_coalesces(tmp_path, monkeypatch):
    """A burst of rapid mutations coalesces into a single snapshot after flush."""
    # Drain any pending timer left over from earlier tests before spying.
    flush_pending_sync()

    vault_dir = _make_vault(tmp_path, monkeypatch)

    # Long debounce so the timer never fires during the burst; the write only
    # happens when we flush explicitly.
    monkeypatch.setattr(config, "state_sync_debounce_seconds", lambda: 30.0)

    # Spy on the synchronous write primitive to count actual file writes.
    real_sync = sync_mod.sync_state_to_vault
    calls = {"n": 0}

    def counting_sync():
        calls["n"] += 1
        real_sync()

    monkeypatch.setattr(sync_mod, "sync_state_to_vault", counting_sync)

    # A burst of mutations. memory_save / task_add each call schedule_sync_to_vault
    # internally; the extra explicit schedule calls simulate more rapid activity.
    memory_save(
        content="Debounced content", title="Debounce Note", tags=["dbtag"], author="db-author"
    )
    task_add(title="Debounce Task", notes="claimed-by: db-agent\n", priority="med")
    for _ in range(5):
        schedule_sync_to_vault()

    # Nothing should have been written yet — all calls collapsed into one pending timer.
    assert calls["n"] == 0
    assert not (vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")).exists()

    # Flush collapses the whole burst into exactly one synchronous write.
    flush_pending_sync()
    assert calls["n"] == 1

    # Final content is correct and atomically written (valid JSON, no torn files).
    mem_txt = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")
    tasks_txt = vault_dir / "Logs" / config.state_mirror_name("state_tasks", ".txt")
    assert mem_txt.exists() and tasks_txt.exists()
    memories = json.loads(mem_txt.read_text(encoding="utf-8"))
    tasks = json.loads(tasks_txt.read_text(encoding="utf-8"))
    assert any(m["title"] == "Debounce Note" for m in memories)
    assert any(t["title"] == "Debounce Task" for t in tasks)

    # Atomic-write guarantee: no leftover temp files in the Logs dir.
    leftovers = [p.name for p in (vault_dir / "Logs").iterdir() if ".tmp" in p.name]
    assert leftovers == []

    # A second flush with nothing pending is a no-op (no extra write).
    flush_pending_sync()
    assert calls["n"] == 1


def test_schedule_disabled_switch(tmp_path, monkeypatch):
    """The UA_STATE_SYNC off-switch suppresses scheduled writes too."""
    flush_pending_sync()
    vault_dir = _make_vault(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_STATE_SYNC", "0")
    monkeypatch.setattr(config, "state_sync_debounce_seconds", lambda: 30.0)

    schedule_sync_to_vault()
    flush_pending_sync()

    assert not (vault_dir / "Logs" / config.state_mirror_name("state_memory", ".md")).exists()
    assert not (vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")).exists()


def test_schedule_zero_debounce_is_synchronous(tmp_path, monkeypatch):
    """A non-positive debounce window falls back to a synchronous write."""
    flush_pending_sync()
    vault_dir = _make_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "state_sync_debounce_seconds", lambda: 0.0)

    memory_save(content="Sync now", title="Immediate", tags=["now"], author="a")
    schedule_sync_to_vault()

    # No flush needed: debounce<=0 writes inline.
    mem_txt = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")
    assert mem_txt.exists()
    memories = json.loads(mem_txt.read_text(encoding="utf-8"))
    assert any(m["title"] == "Immediate" for m in memories)


def test_flush_waits_for_in_flight_timer_write(tmp_path, monkeypatch):
    """flush_pending_sync is a barrier: a timer callback that already consumed
    the pending flag but is still writing must finish before flush returns,
    and the burst still collapses to exactly one write (the old flake: the
    in-flight write landed after flush, torn against the next test's vault)."""
    import threading
    import time

    flush_pending_sync()
    _make_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "state_sync_debounce_seconds", lambda: 0.05)

    calls = {"n": 0}
    write_started = threading.Event()

    def slow_sync():
        write_started.set()
        # Long enough that a flush returning early would read calls["n"] == 0.
        time.sleep(0.05)
        calls["n"] += 1

    monkeypatch.setattr(sync_mod, "sync_state_to_vault", slow_sync)

    schedule_sync_to_vault()
    # Wait until the timer callback is inside its (slow) write, past the point
    # where the debounce state can see it.
    assert write_started.wait(timeout=2.0), "timer never fired"
    flush_pending_sync()
    # Barrier semantics: the in-flight write completed before flush returned,
    # and flush did not issue a second one.
    assert calls["n"] == 1


def test_flush_barrier_covers_consumed_but_unstarted_write(tmp_path, monkeypatch):
    """The residual race behind the 2026-07 re-flake of the debounce tests:
    the timer callback has consumed the pending flag but the OS deschedules it
    before it reaches the write lock. A concurrent flush must not slip between
    the two locks and return early — the escaped write would land under the
    NEXT test's monkeypatches (spurious spy counts / stray state files).

    We make that descheduling deterministic by stalling the timer thread at
    the write-lock acquire point. With the atomic debounce->write lock
    handover, flush blocks until the stalled write completes."""
    import threading
    import time

    flush_pending_sync()
    _make_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "state_sync_debounce_seconds", lambda: 0.01)

    real_lock = sync_mod._write_lock
    reached_write_lock = threading.Event()

    class StallingLock:
        def _stall(self):
            if threading.current_thread() is not threading.main_thread():
                reached_write_lock.set()
                time.sleep(0.05)

        def acquire(self, *a, **k):
            self._stall()
            return real_lock.acquire(*a, **k)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    monkeypatch.setattr(sync_mod, "_write_lock", StallingLock())

    calls = {"n": 0}
    monkeypatch.setattr(
        sync_mod, "sync_state_to_vault", lambda: calls.__setitem__("n", calls["n"] + 1)
    )

    schedule_sync_to_vault()
    assert reached_write_lock.wait(timeout=2.0), "timer never fired"
    flush_pending_sync()
    # The consumed-but-stalled write landed BEFORE flush returned...
    assert calls["n"] == 1
    # ...and nothing escapes afterwards (the debounce is 0.01 s).
    time.sleep(0.05)
    assert calls["n"] == 1


def test_sync_custom_state_sync_dir(tmp_path, monkeypatch):
    """UA_STATE_SYNC_DIR redirects state snapshots to a namespaced vault subdirectory."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "00 - Inbox").mkdir()
    (vault_dir / "Logs").mkdir()
    laptop_logs = vault_dir / "Logs" / "Laptop"
    laptop_logs.mkdir()

    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    monkeypatch.setattr(config, "vault_write_dirs", lambda: ["00 - Inbox", "Logs", "Wiki"])
    monkeypatch.setenv("UA_STATE_SYNC_DIR", "Logs/Laptop")

    memory_save(
        content="Laptop memory note", title="Laptop Note", tags=["laptop"], author="Laptop_agent3"
    )
    sync_state_to_vault()

    mem_txt = laptop_logs / config.state_mirror_name("state_memory", ".txt")
    mem_md = laptop_logs / config.state_mirror_name("state_memory", ".md")
    tasks_txt = laptop_logs / config.state_mirror_name("state_tasks", ".txt")
    tasks_md = laptop_logs / config.state_mirror_name("state_tasks", ".md")

    assert mem_txt.exists()
    assert mem_md.exists()
    assert tasks_txt.exists()
    assert tasks_md.exists()
    assert "Laptop Note" in mem_md.read_text(encoding="utf-8")


def test_cap_md_oversize_snapshot(tmp_path, monkeypatch):
    """Oversized .md mirrors get capped below Obsidian's OOM threshold; .txt stays full."""
    from unified_agent.sync import MD_MIRROR_CAP_BYTES, _cap_md

    assert _cap_md("small") == "small"
    big = "# Offline Memory Snapshot\n" + ("x" * 200 + "\n") * 20_000
    capped = _cap_md(big)
    assert len(capped.encode()) < MD_MIRROR_CAP_BYTES + 10_000
    assert "Truncated by unified-agent" in capped

    vault_dir = _make_vault(tmp_path, monkeypatch)
    for i in range(3):
        memory_save(content="filler " * 200, title=f"Cap Note {i}", tags=["cap"], author="cap")
    # Force the cap low so the fixture-sized mirror actually trips it.
    monkeypatch.setattr(sync_mod, "MD_MIRROR_CAP_BYTES", 5000)
    sync_state_to_vault()

    mem_md = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".md")
    mem_txt = vault_dir / "Logs" / config.state_mirror_name("state_memory", ".txt")
    assert len(mem_md.read_text(encoding="utf-8").encode()) < 6000
    assert "Truncated by unified-agent" in mem_md.read_text(encoding="utf-8")
    # Machine-readable mirror is never capped.
    assert len(json.loads(mem_txt.read_text(encoding="utf-8"))) == 3


def test_cap_md_keeps_frontmatter_and_flags_truncation_at_the_top():
    """Task #1920: a capped mirror kept a duplicated first `---`, so its frontmatter
    broke, and said it was truncated only at the very end."""
    from unified_agent.sync import MD_MIRROR_CAP_BYTES, _cap_md

    body = ("x" * 200 + "\n") * 20_000
    text = "---\ntype: snapshot\ngenerated: now\n---\n\n# Offline Memory Snapshot\n" + body
    capped = _cap_md(text, "state_memory.host.txt")
    assert len(capped.encode()) <= MD_MIRROR_CAP_BYTES
    lines = capped.split("\n")
    assert lines[:4] == ["---", "type: snapshot", "generated: now", "---"]
    assert lines[4] == "" and "Truncated by unified-agent" in lines[5]
    assert "`state_memory.host.txt`" in lines[5]
    assert lines[-1] == lines[5]
    assert "# Offline Memory Snapshot" in capped
    # Text without frontmatter still gets the notice first.
    assert _cap_md(body).split("\n")[1].startswith("**[Truncated")
