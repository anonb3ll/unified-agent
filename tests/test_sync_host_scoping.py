"""Host-scoped state mirrors (task #172).

`Logs/state_memory.md` and friends are *derived*: every machine regenerates
them from its own memory.db. Two machines, one filename, and a sync layer with
no merge strategy for markdown produced "(Conflicted copy <host> <time>)"
duplicates of the largest generated files in the vault, polluting vault_search
with obsolete snapshots.

`UA_STATE_SYNC_DIR` already existed to head this off by putting each device in
its own directory — and it failed for the only reason that matters: it is
opt-in, and nobody set it. The scoping has to be automatic, derived from the
identity already stamped on every write (`config.current_source_host`).
"""

import json

import pytest

from unified_agent import config, storage, sync
from unified_agent.tools import memory as memory_tool
from unified_agent.tools import state_verify as state_verify_tool
from unified_agent.tools import tasks as tasks_tool


@pytest.fixture
def vault(isolated_env, monkeypatch):
    (isolated_env.parent / "vault" / "Logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UA_STATE_SYNC_DEBOUNCE", "0")
    storage._connection = None
    storage.init_db()
    # Opt in to state sync only *after* init_db: its startup snapshot would
    # otherwise land here before the test body sets UA_SOURCE_HOST, filed under
    # the machine's real hostname (#201 — see the conftest comment). conftest
    # joins init_db's background threads, so by the time the switch flips that
    # snapshot has already been skipped.
    monkeypatch.setenv("UA_STATE_SYNC", "1")
    # Setup must hand the test an empty Logs dir. Assert it at the source, so
    # the next leak reads as "the fixture leaked" rather than as an off-by-one
    # three lines into an unrelated assertion.
    assert _logs(isolated_env.parent / "vault") == [], (
        "a state snapshot leaked into the vault during fixture setup"
    )
    yield isolated_env.parent / "vault"


def _logs(vault):
    return sorted(p.name for p in (vault / "Logs").iterdir() if p.is_file())


def test_mirror_filenames_carry_the_source_host(vault, monkeypatch):
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    memory_tool.memory_save(title="a note", content="body", tags=["t"])

    sync.sync_state_to_vault()

    names = _logs(vault)
    assert "state_memory.DesktopHost.md" in names
    assert "state_memory.DesktopHost.txt" in names
    assert "state_tasks.DesktopHost.md" in names
    assert "state_tasks.DesktopHost.txt" in names
    # The colliding names are exactly what must no longer be written.
    assert "state_memory.md" not in names
    assert "state_tasks.md" not in names


def test_two_hosts_writing_the_same_vault_cannot_collide(vault, monkeypatch):
    """The actual defect: one filename, two writers, no merge strategy."""
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    memory_tool.memory_save(title="desktop note", content="from the desktop", tags=["t"])
    sync.sync_state_to_vault()
    desktop = (vault / "Logs" / "state_memory.DesktopHost.md").read_text()

    monkeypatch.setenv("UA_SOURCE_HOST", "LaptopUbuntu")
    sync.sync_state_to_vault()

    # Both mirrors exist side by side; neither host overwrote the other's file,
    # which is the collision Obsidian Sync had no way to merge.
    assert (vault / "Logs" / "state_memory.LaptopUbuntu.md").exists()
    assert (vault / "Logs" / "state_memory.DesktopHost.md").read_text() == desktop
    assert len([n for n in _logs(vault) if n.startswith("state_memory.")]) == 4


def test_a_hostname_cannot_escape_the_logs_directory(vault, monkeypatch):
    """The host string reaches a filename, so it is untrusted input."""
    monkeypatch.setenv("UA_SOURCE_HOST", "../../etc/passwd")

    sync.sync_state_to_vault()

    for name in _logs(vault):
        assert ".." not in name
        assert "/" not in name
    assert not (vault.parent / "etc").exists()
    assert any(n.startswith("state_memory.") for n in _logs(vault))


def test_a_blank_hostname_still_produces_a_usable_name(vault, monkeypatch):
    monkeypatch.setenv("UA_SOURCE_HOST", "   ")

    sync.sync_state_to_vault()

    names = _logs(vault)
    assert "state_memory.unknown-host.md" in names


def test_state_verify_reads_this_hosts_mirror(vault, monkeypatch):
    """The reader must resolve, or drift detection reports permanent drift."""
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    tasks_tool.task_add(title="a task")
    sync.sync_state_to_vault()

    res = state_verify_tool.state_sync_verify()

    assert res["in_sync"] is True


def test_state_verify_falls_back_to_a_legacy_unscoped_mirror(vault, monkeypatch):
    """An existing vault must keep verifying before the first new write.

    Without the fallback, upgrading would report drift against files that are
    present and correct, and trigger a resync on every call.
    """
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    t = tasks_tool.task_add(title="a task")
    m = memory_tool.memory_save(title="a note", content="body", tags=["t"])

    logs = vault / "Logs"
    (logs / "state_tasks.txt").write_text(json.dumps([{"id": t["id"]}]))
    (logs / "state_memory.txt").write_text(json.dumps([{"id": m["id"]}]))

    res = state_verify_tool.state_sync_verify()

    assert res["in_sync"] is True


def test_the_host_scoped_mirror_wins_over_a_stale_legacy_one(vault, monkeypatch):
    """Fallback is a fallback: it must never mask the current file."""
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    tasks_tool.task_add(title="a task")
    sync.sync_state_to_vault()

    # A legacy file left over from before the change, now badly out of date.
    (vault / "Logs" / "state_tasks.txt").write_text(json.dumps([{"id": 9999}]))
    (vault / "Logs" / "state_memory.txt").write_text(json.dumps([{"id": "gone"}]))

    res = state_verify_tool.state_sync_verify()

    assert res["in_sync"] is True


def test_stale_temp_files_from_killed_writes_are_swept(vault, monkeypatch):
    """The second defect: interrupted atomic writes orphan their mkstemp file.

    Four such artifacts (~595 KB of partial snapshots) were found in the live
    vault on 2026-07-27, the oldest nine days old. os.replace never ran, so
    nothing ever cleaned them up and they accumulated forever.
    """
    import os
    import time

    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    logs = vault / "Logs"
    stale = logs / "state_memory.DesktopHost.md.tmpABC123"
    stale.write_text("half a snapshot from a process that was killed")
    old = time.time() - 3600 * 24
    os.utime(stale, (old, old))

    sync.sync_state_to_vault()

    assert not stale.exists()
    assert (logs / "state_memory.DesktopHost.md").exists()


def test_a_recent_temp_file_is_left_alone(vault, monkeypatch):
    """A concurrent writer's in-flight temp file must not be deleted.

    An atomic write takes milliseconds; anything recent belongs to a live
    process, and removing it would break that write rather than clean up
    after a dead one.
    """
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    logs = vault / "Logs"
    in_flight = logs / "state_memory.DesktopHost.md.tmpLIVE99"
    in_flight.write_text("another process is writing this right now")

    sync.sync_state_to_vault()

    assert in_flight.exists()


def test_the_sweep_only_touches_our_own_derived_temp_files(vault, monkeypatch):
    """Scoped to the known derived filenames, per the task's own boundary."""
    import os
    import time

    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    logs = vault / "Logs"
    bystanders = [
        logs / "2026-07-27.md",
        logs / "state_memory.DesktopHost.md",
        logs / "notes.md.tmpXYZ",
    ]
    for p in bystanders:
        p.write_text("content that must survive")
        old = time.time() - 3600 * 24
        os.utime(p, (old, old))

    sync.sync_state_to_vault()

    assert (logs / "2026-07-27.md").read_text() == "content that must survive"
    assert (logs / "notes.md.tmpXYZ").read_text() == "content that must survive"
    # The real mirror is rewritten, not deleted.
    assert (logs / "state_memory.DesktopHost.md").exists()


def test_mirror_name_helper_is_stable_for_a_given_host(monkeypatch):
    monkeypatch.setenv("UA_SOURCE_HOST", "DesktopHost")
    assert config.state_mirror_name("state_memory", ".md") == "state_memory.DesktopHost.md"
    assert config.state_mirror_name("state_tasks", ".txt") == "state_tasks.DesktopHost.txt"


def test_sync_dir_is_unaffected_by_ua_location(vault, monkeypatch):
    monkeypatch.setenv("UA_LOCATION", "laptop")
    monkeypatch.setenv("UA_SOURCE_HOST", "LaptopMac")

    sync.sync_state_to_vault()

    assert (vault / "Logs" / "state_memory.LaptopMac.md").exists()
    assert not (vault / "Logs" / "Laptop" / "state_memory.LaptopMac.md").exists()


def test_state_sync_dir_override_still_applies(vault, monkeypatch):
    monkeypatch.setenv("UA_STATE_SYNC_DIR", "Logs/Custom")
    monkeypatch.setenv("UA_SOURCE_HOST", "SomeHost")

    sync.sync_state_to_vault()

    assert (vault / "Logs" / "Custom" / "state_memory.SomeHost.md").exists()
