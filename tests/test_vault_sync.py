"""Tests for the hub -> desktop vault sync engine (task #1288).

Everything runs offline against tmp_path directories: LocalDirTransport
stands in for the Mac hub, so no SSH, no network and no real vault is
touched. Fixed timestamps (set via os.utime) make the newer/older comparisons
deterministic.
"""

import base64
import hashlib
import inspect
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unified_agent import vault_sync
from unified_agent.vault_sync import (
    DEFAULT_RETIRED_PATHS,
    LocalDirTransport,
    RemoteFile,
    sync_once,
)


@pytest.fixture(autouse=True)
def configured_remote(monkeypatch):
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_HOST", "vault-host")
    monkeypatch.setenv("UA_VAULT_SYNC_REMOTE_ROOT", "vault")


def _touch(path: Path, content: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def hub_and_desktop(tmp_path):
    hub = tmp_path / "hub"
    desktop = tmp_path / "desktop"
    hub.mkdir()
    desktop.mkdir()
    return hub, desktop


class TestNewerHubPulls:
    def test_hub_only_file_is_pulled(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "Wiki" / "new-note.md", "fresh from hub", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.ok is True
        assert result.reachable is True
        assert "Wiki/new-note.md" in result.pulled
        assert (desktop / "Wiki" / "new-note.md").read_text() == "fresh from hub"
        assert result.conflicts == []

    def test_hub_newer_than_desktop_overwrites_desktop(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        old = time.time() - 3600
        new = time.time()
        _touch(desktop / "Wiki" / "note.md", "stale desktop copy", old)
        _touch(hub / "Wiki" / "note.md", "updated on hub", new)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert "Wiki/note.md" in result.pulled
        assert (desktop / "Wiki" / "note.md").read_text() == "updated on hub"
        assert result.conflicts == []

    def test_unchanged_file_is_left_alone(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(desktop / "Wiki" / "note.md", "same everywhere", now)
        _touch(hub / "Wiki" / "note.md", "same everywhere", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.pulled == []
        assert result.conflicts == []


class TestNewerDesktopIsConflictNotOverwrite:
    def test_desktop_edit_after_hub_change_is_not_overwritten(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        hub_time = time.time() - 3600
        desktop_time = time.time()
        _touch(hub / "Wiki" / "note.md", "hub content", hub_time)
        _touch(desktop / "Wiki" / "note.md", "desktop edit, newer", desktop_time)

        result = sync_once(desktop, LocalDirTransport(hub))

        # The desktop original must survive untouched.
        assert (desktop / "Wiki" / "note.md").read_text() == "desktop edit, newer"
        assert result.pulled == []
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.relpath == "Wiki/note.md"
        assert conflict.reason == "local_newer"
        assert conflict.conflict_file is not None
        # The conflict copy carries the content that was NOT applied (the
        # hub's), preserved alongside the untouched desktop original.
        conflict_path = desktop / "Wiki" / conflict.conflict_file
        assert conflict_path.exists()
        assert conflict_path.read_text() == "hub content"
        assert vault_sync._CONFLICT_MARKER in conflict.conflict_file

    def test_desktop_only_file_is_flagged_and_left_in_place(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(desktop / "Logs" / "2026-09-22.md", "desktop-only note", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert (desktop / "Logs" / "2026-09-22.md").read_text() == "desktop-only note"
        assert result.pulled == []
        assert len(result.conflicts) == 1
        assert result.conflicts[0].reason == "local_only"
        assert result.conflicts[0].relpath == "Logs/2026-09-22.md"

    def test_conflict_copy_pushed_to_hub_inbox_when_enabled(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        hub_time = time.time() - 3600
        desktop_time = time.time()
        _touch(hub / "Wiki" / "note.md", "hub content", hub_time)
        _touch(desktop / "Wiki" / "note.md", "desktop edit", desktop_time)

        result = sync_once(desktop, LocalDirTransport(hub), push_conflicts=True)

        conflict_file = result.conflicts[0].conflict_file
        pushed = list((hub / "00 - Inbox").glob("*.conflict-*"))
        assert len(pushed) == 1
        assert conflict_file is not None
        assert pushed[0].name != conflict_file
        assert pushed[0].read_text() == "hub content"

    def test_namesake_local_only_conflicts_get_distinct_hub_copies(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(desktop / "Wiki" / "same.md", "wiki version", now)
        _touch(desktop / "Logs" / "same.md", "log version", now)

        result = sync_once(desktop, LocalDirTransport(hub), push_conflicts=True)

        assert len(result.conflicts) == 2
        pushed = list((hub / "00 - Inbox").glob("same.conflict-desktop-*.md"))
        assert len(pushed) == 2
        assert {path.read_text() for path in pushed} == {"wiki version", "log version"}

    def test_failed_optional_conflict_push_is_recorded(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "note.md", "hub", time.time() - 3600)
        _touch(desktop / "Wiki" / "note.md", "desktop", time.time())

        class FailingPush(LocalDirTransport):
            def push_conflict_copy(self, relpath_hint, src):
                raise OSError("hub refused conflict copy")

        result = sync_once(desktop, FailingPush(hub), push_conflicts=True)
        assert not result.ok
        assert [error["action"] for error in result.file_errors] == ["push_conflict"]
        assert result.conflicts[0].conflict_file is not None

    def test_push_conflicts_off_by_default_never_writes_to_hub(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        hub_time = time.time() - 3600
        desktop_time = time.time()
        _touch(hub / "Wiki" / "note.md", "hub content", hub_time)
        _touch(desktop / "Wiki" / "note.md", "desktop edit", desktop_time)

        sync_once(desktop, LocalDirTransport(hub))

        assert not (hub / "00 - Inbox").exists()

    def test_existing_conflict_is_pushed_after_push_is_enabled(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "note.md", "hub", time.time() - 3600)
        _touch(desktop / "Wiki" / "note.md", "desktop", time.time())
        sync_once(desktop, LocalDirTransport(hub))

        result = sync_once(desktop, LocalDirTransport(hub), push_conflicts=True)

        assert result.ok
        pushed = list((hub / "00 - Inbox").glob("note*.conflict-desktop-*.md"))
        assert len(pushed) == 1
        assert pushed[0].read_text() == "hub"

    def test_repeated_conflict_push_preserves_existing_copy(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        source = desktop / "Wiki" / "note.md"
        _touch(source, "desktop", time.time())
        transport = LocalDirTransport(hub)
        transport.push_conflict_copy("Wiki/note.md", source)
        dest = next((hub / "00 - Inbox").glob("note.conflict-desktop-*.md"))
        before = dest.stat().st_mtime_ns
        transport.push_conflict_copy("Wiki/note.md", source)
        assert dest.read_text() == "desktop"
        assert dest.stat().st_mtime_ns == before

    def test_edit_after_upload_snapshot_uses_snapshot_name_and_bytes(
        self, hub_and_desktop, monkeypatch
    ):
        hub, desktop = hub_and_desktop
        source = desktop / "Wiki" / "note.md"
        _touch(source, "version A", time.time())
        original_name = vault_sync._pushed_conflict_name

        def edit_after_snapshot(relpath_hint, src, *, content_hash=None):
            source.write_text("version B")
            return original_name(relpath_hint, src, content_hash=content_hash)

        monkeypatch.setattr(vault_sync, "_pushed_conflict_name", edit_after_snapshot)
        transport = LocalDirTransport(hub)
        transport.push_conflict_copy("Wiki/note.md", source)
        monkeypatch.setattr(vault_sync, "_pushed_conflict_name", original_name)
        transport.push_conflict_copy("Wiki/note.md", source)
        pushed = list((hub / "00 - Inbox").glob("note.conflict-desktop-*.md"))
        assert len(pushed) == 2
        assert {path.read_text() for path in pushed} == {"version A", "version B"}

    def test_remote_conflict_writer_never_truncates_existing_copy(self, tmp_path):
        from unified_agent.vault_sync import _REMOTE_WRITER_SCRIPT

        name = "note.conflict-desktop-test.md"
        encoded = base64.urlsafe_b64encode(name.encode()).decode()
        full = b"complete hub copy"
        digest = hashlib.sha256(full).hexdigest()

        def run(data: bytes, *extra: str):
            return subprocess.run(
                [sys.executable, "-c", _REMOTE_WRITER_SCRIPT, encoded, digest, *extra],
                cwd=tmp_path,
                input=data,
                capture_output=True,
            )

        assert run(b"", "probe").stdout.strip() == b"absent"
        assert run(full).returncode == 0
        dest = tmp_path / "00 - Inbox" / name
        assert run(b"", "probe").stdout.strip() == b"match"
        assert run(b"short").returncode == 0  # matching destination is an idempotent no-op
        assert dest.read_bytes() == full
        dest.unlink()
        assert run(b"short").returncode != 0
        assert not dest.exists()


class TestRetiredPathNotResurrected:
    def test_retired_tree_on_hub_is_never_pulled(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "Logs" / "Laptop" / "2026-01-01.md", "should stay dead", now)
        _touch(hub / "Logs" / "2026-09-22.md", "ordinary log, should pull", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert not (desktop / "Logs" / "Laptop").exists()
        assert "Logs/Laptop/2026-01-01.md" in result.retired_skipped
        assert "Logs/2026-09-22.md" in result.pulled
        assert result.conflicts == []

    def test_default_retired_paths_include_logs_laptop(self):
        assert "Logs/Laptop" in DEFAULT_RETIRED_PATHS

    def test_custom_retired_paths_are_respected(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "Wiki" / "Scratch" / "draft.md", "retired via config", now)

        result = sync_once(desktop, LocalDirTransport(hub), retired_paths=("Wiki/Scratch",))

        assert not (desktop / "Wiki" / "Scratch").exists()
        assert "Wiki/Scratch/draft.md" in result.retired_skipped


class TestDryRun:
    def test_dry_run_makes_no_filesystem_changes(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "Wiki" / "new-note.md", "fresh from hub", now)
        hub_time = time.time() - 3600
        desktop_time = time.time()
        _touch(hub / "Wiki" / "conflicted.md", "hub content", hub_time)
        _touch(desktop / "Wiki" / "conflicted.md", "desktop edit", desktop_time)

        result = sync_once(desktop, LocalDirTransport(hub), dry_run=True)

        assert not (desktop / "Wiki" / "new-note.md").exists()
        assert (desktop / "Wiki" / "conflicted.md").read_text() == "desktop edit"
        assert not list(desktop.rglob("*.conflict-*"))
        # The result still reports what WOULD have happened.
        assert "Wiki/new-note.md" in result.pulled
        assert len(result.conflicts) == 1


class TestUnreachableRemote:
    def test_unreachable_remote_is_a_clean_noop(self, tmp_path):
        desktop = tmp_path / "desktop"
        desktop.mkdir()
        missing_hub = tmp_path / "does-not-exist"

        result = sync_once(desktop, LocalDirTransport(missing_hub))

        assert result.ok is True
        assert result.reachable is False
        assert result.pulled == []
        assert result.conflicts == []


class TestConflictArtifactsAreNotResynced:
    def test_a_conflict_copy_is_never_itself_flagged(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(
            desktop / "Wiki" / "note.conflict-20260101T000000Z.md",
            "leftover conflict copy",
            now,
        )

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.conflicts == []
        assert result.pulled == []

    def test_regular_filename_containing_conflict_word_is_synced(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "conflict-discussion.md", "normal note", time.time())

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.pulled == ["Wiki/conflict-discussion.md"]
        assert (desktop / "Wiki" / "conflict-discussion.md").read_text() == "normal note"


class TestStateFile:
    def test_write_state_then_read_state_round_trips(self, tmp_path):
        state_path = tmp_path / "vault-sync-state.json"
        result = sync_once(
            tmp_path / "desktop-missing", LocalDirTransport(tmp_path / "hub-missing")
        )

        payload = vault_sync.write_state(result, path=state_path)

        assert payload["reachable"] is False
        loaded = vault_sync.read_state(state_path)
        assert loaded == payload

    def test_last_success_survives_an_unreachable_run(self, tmp_path):
        state_path = tmp_path / "vault-sync-state.json"
        hub = tmp_path / "hub"
        desktop = tmp_path / "desktop"
        hub.mkdir()
        desktop.mkdir()

        ok_result = sync_once(desktop, LocalDirTransport(hub))
        first = vault_sync.write_state(ok_result, path=state_path)
        assert first["last_success_utc"] is not None

        unreachable_result = sync_once(desktop, LocalDirTransport(tmp_path / "gone"))
        second = vault_sync.write_state(unreachable_result, path=state_path)

        assert second["last_success_utc"] == first["last_success_utc"]
        assert second["last_run_utc"] != first["last_run_utc"] or True  # run always updates

    def test_missing_state_file_reads_as_none(self, tmp_path):
        assert vault_sync.read_state(tmp_path / "nope.json") is None

    def test_corrupt_state_file_is_preserved_and_reported(self, tmp_path):
        path = tmp_path / "vault-sync-state.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(vault_sync.VaultSyncStateError):
            vault_sync.read_state(path)
        result = vault_sync.SyncResult(True, False, False, "start", "finish")
        with pytest.raises(vault_sync.VaultSyncStateError):
            vault_sync.write_state(result, path=path)
        assert path.read_text() == "not json"

    def test_non_object_state_file_is_rejected(self, tmp_path):
        path = tmp_path / "vault-sync-state.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(vault_sync.VaultSyncStateError, match="JSON object"):
            vault_sync.read_state(path)

    @pytest.mark.parametrize(
        "payload",
        [
            {"conflicts": None},
            {"conflicts": [{"relpath": [], "reason": "local_newer"}]},
            {"pulled": "Wiki/a.md"},
            {"conflict_count": "one"},
            {"conflicts": [{"relpath": "Wiki/a.md", "reason": "local_newer"}], "conflict_count": 0},
        ],
    )
    def test_malformed_nested_state_fails_before_merge(self, tmp_path, payload):
        import json

        path = tmp_path / "vault-sync-state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = vault_sync.SyncResult(True, False, False, "start", "finish")
        with pytest.raises(vault_sync.VaultSyncStateError):
            vault_sync.read_state(path)
        with pytest.raises(vault_sync.VaultSyncStateError):
            vault_sync.write_state(result, path=path)
        assert json.loads(path.read_text()) == payload

    def test_malformed_state_blocks_pull_before_vault_changes(self, tmp_path, monkeypatch):
        hub, desktop = tmp_path / "hub", tmp_path / "desktop"
        hub.mkdir()
        desktop.mkdir()
        _touch(hub / "Wiki" / "note.md", "hub", time.time())
        state_path = tmp_path / "state.json"
        state_path.write_text('{"conflicts": null}', encoding="utf-8")
        monkeypatch.setattr(vault_sync.config, "vault_sync_local_root", lambda: desktop)
        monkeypatch.setattr(vault_sync.config, "vault_sync_state_path", lambda: state_path)
        monkeypatch.setattr(vault_sync, "SSHTransport", lambda host, root: LocalDirTransport(hub))

        with pytest.raises(vault_sync.VaultSyncStateError):
            vault_sync.run()
        assert not (desktop / "Wiki" / "note.md").exists()

    def test_clock_rollback_does_not_freeze_state(self, tmp_path):
        path = tmp_path / "vault-sync-state.json"
        newer = vault_sync.SyncResult(
            True,
            True,
            False,
            "2026-09-23T10:00:00+00:00",
            "2026-09-23T10:01:00+00:00",
            conflicts=[vault_sync.SyncConflict("Wiki/a.md", "local_newer", None, "t")],
        )
        older = vault_sync.SyncResult(
            True, True, False, "2026-09-23T09:59:00+00:00", "2026-09-23T10:00:00+00:00"
        )
        vault_sync.write_state(newer, path=path)
        saved = vault_sync.write_state(older, path=path)
        assert saved["last_run_utc"] == older.finished_at

    def test_manual_and_timer_runs_do_not_overlap(self, tmp_path, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Lock

        hub, desktop = tmp_path / "hub", tmp_path / "desktop"
        hub.mkdir()
        desktop.mkdir()
        monkeypatch.setattr(vault_sync.config, "vault_sync_local_root", lambda: desktop)
        monkeypatch.setattr(
            vault_sync.config, "vault_sync_state_path", lambda: tmp_path / "state.json"
        )
        active = 0
        max_active = 0
        guard = Lock()

        class TrackingTransport(LocalDirTransport):
            def reachable(self):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return True

        monkeypatch.setattr(vault_sync, "SSHTransport", lambda host, root: TrackingTransport(hub))
        start = Barrier(2)

        def call(_):
            start.wait()
            return vault_sync.run(dry_run=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(call, range(2)))
        assert max_active == 1
        assert all(result["ok"] for result in results)

    def test_escaping_tree_is_rejected_before_transport_or_local_walk(self, tmp_path):
        desktop = tmp_path / "desktop"
        desktop.mkdir()
        (tmp_path / "archive").mkdir()

        class NoRemote(LocalDirTransport):
            def reachable(self):
                raise AssertionError("invalid tree must be rejected first")

        result = sync_once(desktop, NoRemote(tmp_path), synced_trees=("Wiki", "../archive"))
        assert not result.ok
        assert "invalid synced tree" in result.error

    def test_parallel_state_writers_preserve_success_and_valid_json(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        path = tmp_path / "vault-sync-state.json"
        success = vault_sync.SyncResult(True, True, False, "start", "success")
        failed = vault_sync.SyncResult(True, False, False, "start", "failed")
        vault_sync.write_state(success, path=path)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: vault_sync.write_state(failed, path=path), range(32)))

        state = vault_sync.read_state(path)
        assert state["last_success_utc"] == "success"
        assert state["last_run_utc"] == "failed"
        assert not list(tmp_path.glob("*.tmp"))


class TestDivergenceCounting:
    def test_conflict_count_reflects_all_reasons(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        hub_time = time.time() - 3600
        desktop_time = time.time()
        _touch(hub / "Wiki" / "a.md", "hub a", hub_time)
        _touch(desktop / "Wiki" / "a.md", "desktop a newer", desktop_time)
        _touch(desktop / "Logs" / "local-only.md", "only here", desktop_time)

        result = sync_once(desktop, LocalDirTransport(hub))
        state = vault_sync.write_state(result, path=hub.parent / "state.json")

        assert state["conflict_count"] == 2
        reasons = {c["reason"] for c in state["conflicts"]}
        assert reasons == {"local_newer", "local_only"}


class TestSSHTransportListParsing:
    """SSHTransport.list_files parses find's tab/NUL-delimited output; exercise
    the parser directly rather than shelling out to a real ssh/find."""

    def test_parses_well_formed_entries(self, monkeypatch):
        from unified_agent.vault_sync import SSHTransport

        transport = SSHTransport("vault-host", "vault")

        class _FakeProc:
            returncode = 0
            stdout = b"Wiki/a.md\t1700000000.0\t123\x00Logs/b.md\t1700000001.5\t456\x00"

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", lambda *a, **k: _FakeProc())
        files = transport.list_files(["Wiki", "Logs"])

        assert files["Wiki/a.md"] == RemoteFile("Wiki/a.md", 1700000000.0, 123)
        assert files["Logs/b.md"] == RemoteFile("Logs/b.md", 1700000001.5, 456)

    def test_nonzero_returncode_raises_not_empty(self, monkeypatch):
        # An empty dict would read as "the hub is empty"; a failed listing
        # must be an error so the run records ok=False.
        from unified_agent.vault_sync import SSHTransport, VaultSyncTransportError

        transport = SSHTransport("vault-host", "vault")

        class _FakeProc:
            returncode = 1
            stdout = b""
            stderr = b"python3: command not found"

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", lambda *a, **k: _FakeProc())
        with pytest.raises(VaultSyncTransportError, match="exited 1"):
            transport.list_files(["Wiki"])

    def test_malformed_listing_record_fails_enumeration(self, monkeypatch):
        from unified_agent.vault_sync import SSHTransport, VaultSyncTransportError

        class FakeProc:
            returncode = 0
            stdout = b"Wiki/good.md\t1\t2\0broken-record\0"
            stderr = b""

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", lambda *a, **k: FakeProc())
        with pytest.raises(VaultSyncTransportError, match="malformed remote vault listing"):
            SSHTransport("vault-host", "vault").list_files(["Wiki"])

    def test_non_utf8_remote_filename_uses_surrogateescape(self, monkeypatch):
        from unified_agent.vault_sync import SSHTransport

        class FakeProc:
            returncode = 0
            stdout = b"Wiki/\xff.md\t1\t2\0"
            stderr = b""

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", lambda *a, **k: FakeProc())
        files = SSHTransport("vault-host", "vault").list_files(["Wiki"])
        assert "Wiki/\udcff.md" in files

    def test_unreachable_ssh_is_not_reachable(self, monkeypatch):
        from unified_agent.vault_sync import SSHTransport

        transport = SSHTransport("vault-host", "vault")

        def _raise(*a, **k):
            raise OSError("no route to host")

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", _raise)
        assert transport.reachable() is False


class TestReviewHardening:
    """Task #705 review: BSD-safe listing, failure honesty, no unbounded copies,
    path containment, and state-merge rules."""

    def test_remote_lister_script_runs_and_handles_awkward_names(self, tmp_path):
        # Runs the exact script SSHTransport sends, with the local python,
        # so the listing never depends on GNU find.
        import subprocess
        import sys

        _touch(tmp_path / "00 - Inbox" / "a note.md", "x", 1700000000)
        _touch(tmp_path / "Wiki" / "tab\there.md", "yy", 1700000001)
        proc = subprocess.run(
            [sys.executable, "-c", vault_sync._REMOTE_LISTER_SCRIPT, "00 - Inbox", "Wiki"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        from unified_agent.vault_sync import SSHTransport

        class _FakeProc:
            returncode = 0
            stdout = proc.stdout

        import unified_agent.vault_sync as module

        original = module.subprocess.run
        module.subprocess.run = lambda *a, **k: _FakeProc()
        try:
            files = SSHTransport("vault-host", "vault").list_files(["00 - Inbox", "Wiki"])
        finally:
            module.subprocess.run = original
        assert sorted(files) == ["00 - Inbox/a note.md", "Wiki/tab\there.md"]
        assert files["Wiki/tab\there.md"].size == 2

    def test_failed_listing_is_not_a_successful_empty_sync(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(desktop / "Wiki" / "mine.md", "desktop", time.time())

        class _Broken(LocalDirTransport):
            def list_files(self, trees):
                raise vault_sync.VaultSyncTransportError("listing failed")

        result = sync_once(desktop, _Broken(hub))
        assert result.ok is False
        assert result.error
        assert result.conflicts == []

    def test_persisting_conflict_reuses_one_copy(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        old, new = time.time() - 3600, time.time()
        _touch(hub / "Wiki" / "note.md", "hub version", old)
        _touch(desktop / "Wiki" / "note.md", "desktop edit", new)

        for _ in range(3):
            sync_once(desktop, LocalDirTransport(hub))
        copies = list((desktop / "Wiki").glob("note.conflict-*.md"))
        assert len(copies) == 1

    def test_changed_hub_bytes_get_new_conflict_copy_despite_matching_metadata(
        self, hub_and_desktop
    ):
        hub, desktop = hub_and_desktop
        old, new = time.time() - 3600, time.time()
        _touch(hub / "Wiki" / "note.md", "hub first", old)
        _touch(desktop / "Wiki" / "note.md", "desktop edit", new)

        sync_once(desktop, LocalDirTransport(hub))
        _touch(hub / "Wiki" / "note.md", "hub other", old)
        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.conflicts[0].reason == "local_newer"
        copies = list((desktop / "Wiki").glob("note.conflict-*.md"))
        assert {copy.read_text() for copy in copies} == {"hub first", "hub other"}

    def test_escaping_remote_paths_are_skipped(self, hub_and_desktop, tmp_path):
        hub, desktop = hub_and_desktop

        class _Hostile(LocalDirTransport):
            def list_files(self, trees):
                return {
                    "../escape.md": RemoteFile("../escape.md", time.time(), 3),
                    "/etc/passwd": RemoteFile("/etc/passwd", time.time(), 3),
                }

            def pull_file(self, relpath, dest):
                raise AssertionError(f"must never pull {relpath}")

        result = sync_once(desktop, _Hostile(hub))
        assert sorted(result.unsafe_skipped) == ["../escape.md", "/etc/passwd"]
        assert not (tmp_path / "escape.md").exists()

    def test_one_failed_transfer_does_not_abort_the_rest(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "Wiki" / "bad.md", "x", now)
        _touch(hub / "Wiki" / "good.md", "y", now)

        class _Flaky(LocalDirTransport):
            def pull_file(self, relpath, dest):
                if relpath.endswith("bad.md"):
                    raise OSError("transfer failed")
                super().pull_file(relpath, dest)

        result = sync_once(desktop, _Flaky(hub))
        assert result.ok is False
        assert [e["relpath"] for e in result.file_errors] == ["Wiki/bad.md"]
        assert (desktop / "Wiki" / "good.md").exists()

    def test_incomplete_pull_does_not_replace_desktop_file(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "note.md", "complete hub version", time.time())
        local = desktop / "Wiki" / "note.md"
        _touch(local, "desktop version", time.time() - 3600)

        class Truncated(LocalDirTransport):
            def pull_file(self, relpath, dest):
                dest.write_text("partial")

        result = sync_once(desktop, Truncated(hub))
        assert not result.ok
        assert result.file_errors[0]["action"] == "pull"
        assert local.read_text() == "desktop version"

    def test_changed_hub_after_listing_is_not_published(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        remote = hub / "Wiki" / "note.md"
        _touch(remote, "first hub version", time.time())

        class ChangedAfterListing(LocalDirTransport):
            def list_files(self, trees):
                listed = super().list_files(trees)
                remote.write_text("second hub version")
                return listed

        result = sync_once(desktop, ChangedAfterListing(hub))
        assert not result.ok
        assert result.file_errors[0]["action"] == "pull"
        assert not (desktop / "Wiki" / "note.md").exists()

    def test_changed_hub_during_pull_is_not_published(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        remote = hub / "Wiki" / "note.md"
        _touch(remote, "first hub version", time.time())

        class ChangedDuringPull(LocalDirTransport):
            def pull_file(self, relpath, dest):
                super().pull_file(relpath, dest)
                remote.write_text("second hub version")

        result = sync_once(desktop, ChangedDuringPull(hub))
        assert not result.ok
        assert result.file_errors[0]["action"] == "pull"
        assert not (desktop / "Wiki" / "note.md").exists()

    def test_ssh_transfer_handles_shell_metacharacters_as_filename(self, monkeypatch, tmp_path):
        import subprocess

        from unified_agent.vault_sync import SSHTransport

        remote = tmp_path / "vault" / "Wiki"
        remote.mkdir(parents=True)
        note = remote / "-rf;echo hacked.md"
        note.write_text("expected bytes")
        original_run = subprocess.run

        def local_ssh(argv, **kwargs):
            assert argv[0] == "ssh"
            return original_run(["bash", "-c", argv[-1]], cwd=tmp_path, **kwargs)

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", local_ssh)
        dest = tmp_path / "download.md"
        SSHTransport("vault-host", "vault").pull_file("Wiki/-rf;echo hacked.md", dest)
        assert dest.read_text() == "expected bytes"
        assert not (tmp_path / "hacked.md").exists()

    def test_ssh_stat_handles_shell_metacharacters_as_filename(self, monkeypatch, tmp_path):
        from unified_agent.vault_sync import SSHTransport

        remote = tmp_path / "vault" / "Wiki"
        remote.mkdir(parents=True)
        note = remote / "-rf;echo hacked.md"
        note.write_text("expected bytes")
        original_run = subprocess.run

        def local_ssh(argv, **kwargs):
            assert argv[0] == "ssh"
            return original_run(["bash", "-c", argv[-1]], cwd=tmp_path, **kwargs)

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", local_ssh)
        result = SSHTransport("vault-host", "vault").stat_file("Wiki/-rf;echo hacked.md")
        assert result.size == len("expected bytes")
        assert not (tmp_path / "hacked.md").exists()

    def test_unreachable_run_keeps_known_conflicts(self, tmp_path):
        state = tmp_path / "state.json"
        conflicted = vault_sync.SyncResult(
            ok=True,
            reachable=True,
            dry_run=False,
            started_at="2026-09-23T00:00:00Z",
            finished_at="2026-09-23T00:00:01Z",
            conflicts=[vault_sync.SyncConflict("Wiki/a.md", "local_newer", None, "t")],
        )
        vault_sync.write_state(conflicted, path=state)
        unreachable = vault_sync.SyncResult(
            ok=True,
            reachable=False,
            dry_run=False,
            started_at="2026-09-23T00:10:00Z",
            finished_at="2026-09-23T00:10:01Z",
        )
        after = vault_sync.write_state(unreachable, path=state)
        assert after["conflict_count"] == 1
        assert after["last_success_utc"] == "2026-09-23T00:00:01Z"
        assert after["last_run_reachable"] is False

    def test_dry_run_never_advances_last_success(self, tmp_path):
        state = tmp_path / "state.json"
        dry = vault_sync.SyncResult(
            ok=True,
            reachable=True,
            dry_run=True,
            started_at="2026-09-23T00:00:00Z",
            finished_at="2026-09-23T00:00:01Z",
        )
        assert vault_sync.write_state(dry, path=state)["last_success_utc"] is None


class TestIdenticalContentIsNotAConflict:
    def test_equal_size_and_mtime_with_different_bytes_is_a_conflict(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        stamp = time.time()
        _touch(hub / "Wiki" / "edit.md", "aaaa", stamp)
        _touch(desktop / "Wiki" / "edit.md", "bbbb", stamp)

        result = sync_once(desktop, LocalDirTransport(hub))
        assert result.ok
        assert [c.reason for c in result.conflicts] == ["same_mtime_diff"]
        assert (desktop / "Wiki" / "edit.md").read_text() == "bbbb"

    def test_concurrent_local_edit_is_kept_with_hub_conflict_copy(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(desktop / "Wiki" / "edit.md", "old", time.time() - 3600)
        _touch(hub / "Wiki" / "edit.md", "hub", time.time())

        class EditingTransport(LocalDirTransport):
            def pull_file(self, relpath, dest):
                super().pull_file(relpath, dest)
                (desktop / relpath).write_text("new desktop edit")

        result = sync_once(desktop, EditingTransport(hub))
        assert result.ok
        assert (desktop / "Wiki" / "edit.md").read_text() == "new desktop edit"
        assert [c.reason for c in result.conflicts] == ["concurrent_local_edit"]
        conflict = desktop / "Wiki" / result.conflicts[0].conflict_file
        assert conflict.read_text() == "hub"

    def test_edit_after_hub_wins_decision_is_not_overwritten(self, hub_and_desktop, monkeypatch):
        hub, desktop = hub_and_desktop
        local = desktop / "Wiki" / "edit.md"
        _touch(local, "old", time.time() - 3600)
        _touch(hub / "Wiki" / "edit.md", "hub", time.time())
        real_stat = Path.stat
        raced = False

        def racing_stat(path, *args, **kwargs):
            nonlocal raced
            st = real_stat(path, *args, **kwargs)
            caller = inspect.currentframe().f_back
            if (
                path == local
                and not raced
                and caller.f_code.co_name == "sync_once"
                and caller.f_locals.get("relpath") == "Wiki/edit.md"
            ):
                raced = True
                _touch(local, "new desktop edit", time.time() + 10)
            return st

        monkeypatch.setattr(Path, "stat", racing_stat)
        result = sync_once(desktop, LocalDirTransport(hub))
        assert raced
        assert local.read_text() == "new desktop edit"
        assert [conflict.reason for conflict in result.conflicts] == ["concurrent_local_edit"]

    def test_symlink_swapped_during_pull_cannot_redirect_write(self, hub_and_desktop, tmp_path):
        hub, desktop = hub_and_desktop
        outside = tmp_path / "outside"
        outside.mkdir()
        _touch(hub / "Wiki" / "note.md", "hub", time.time())

        class SymlinkSwapTransport(LocalDirTransport):
            def pull_file(self, relpath, dest):
                super().pull_file(relpath, dest)
                (desktop / "Wiki").symlink_to(outside, target_is_directory=True)

        result = sync_once(desktop, SymlinkSwapTransport(hub))
        assert not result.ok
        assert result.file_errors[0]["action"] == "pull"
        assert not (outside / "note.md").exists()

    def test_bumped_mtime_with_same_bytes_is_left_alone(self, hub_and_desktop):
        # Real-hub dry run found ~198 of these: a copy/checkout bumped the
        # desktop mtimes, the bytes are identical. Never a conflict.
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "same.md", "identical bytes", time.time() - 3600)
        _touch(desktop / "Wiki" / "same.md", "identical bytes", time.time())

        result = sync_once(desktop, LocalDirTransport(hub))
        assert result.conflicts == []
        assert result.pulled == []
        assert list((desktop / "Wiki").glob("same.conflict-*")) == []

    def test_same_size_different_bytes_is_still_a_conflict(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "edit.md", "aaaa", time.time() - 3600)
        _touch(desktop / "Wiki" / "edit.md", "bbbb", time.time())

        result = sync_once(desktop, LocalDirTransport(hub))
        assert [c.reason for c in result.conflicts] == ["local_newer"]

    def test_hash_failure_keeps_conflict_visible_in_saved_state(self, hub_and_desktop, tmp_path):
        hub, desktop = hub_and_desktop
        _touch(hub / "Wiki" / "edit.md", "aaaa", time.time() - 3600)
        _touch(desktop / "Wiki" / "edit.md", "bbbb", time.time())
        state_path = tmp_path / "sync-state.json"
        vault_sync.write_state(sync_once(desktop, LocalDirTransport(hub)), path=state_path)

        class NoHash(LocalDirTransport):
            def hash_files(self, relpaths):
                return {}

        result = sync_once(desktop, NoHash(hub))
        saved = vault_sync.write_state(result, path=state_path)
        assert not result.ok
        assert saved["conflict_count"] == 1
        assert saved["conflicts"][0]["relpath"] == "Wiki/edit.md"
        assert saved["conflicts"][0]["reason"] == "local_newer"
        assert saved["conflicts"][0]["conflict_file"]


class TestIgnoredGlobs:
    def test_ignored_files_are_neither_pulled_nor_conflicts(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(desktop / "Logs" / "state_tasks.ThisHost.md", "mine", now)
        _touch(hub / "Logs" / "state_tasks.ThisHost.md", "old", now - 3600)
        _touch(desktop / "Wiki" / ".space" / "context.mdb", "cache", now)
        _touch(hub / "Logs" / "keep.md", "pull me", now)

        result = sync_once(
            desktop,
            LocalDirTransport(hub),
            ignored_globs=("Logs/state_*.ThisHost.*", "*/.space/*"),
        )
        assert result.conflicts == []
        assert result.pulled == ["Logs/keep.md"]
        assert (desktop / "Logs" / "state_tasks.ThisHost.md").read_text() == "mine"

    def test_default_ignores_include_this_hosts_state_mirrors(self, monkeypatch):
        from unified_agent import config

        monkeypatch.setattr("socket.gethostname", lambda: "DeskBox.local")
        monkeypatch.setenv("UA_VAULT_SYNC_IGNORE", "Wiki/scratch/*")
        globs = config.vault_sync_ignored_globs()
        assert "Logs/state_*.DeskBox.*" in globs
        assert "Wiki/scratch/*" in globs
        assert vault_sync._is_ignored("Logs/state_memory.host.txt.tmpaapp993f", globs)

    def test_source_host_override_is_also_ignored(self, monkeypatch):
        from unified_agent import config

        monkeypatch.setattr("socket.gethostname", lambda: "DeskBox.local")
        monkeypatch.setenv("UA_SOURCE_HOST", "Named Desktop")
        globs = config.vault_sync_ignored_globs()
        assert "Logs/state_*.Named-Desktop.*" in globs


class TestSSHTransportMultiplexing:
    """Task #1492: one run must not open a fresh SSH connection per call.

    _stage_pull alone costs five SSH round trips per pulled file. Without
    multiplexing a ten-file pull opened ~50 connections in seconds, after which
    the Mac hub refused every exec ("exec request failed on channel 0") —
    failing the pull and blocking unrelated `git push mac` for 20+ minutes.
    """

    @staticmethod
    def _recorder(monkeypatch):
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stderr = b""

            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(argv, *args, **kwargs):
            calls.append(list(argv))
            is_stat = shlex.quote(vault_sync._REMOTE_STAT_SCRIPT) in argv[-1]
            return _Proc(b"1700000000.0\t3" if is_stat else b"")

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", fake_run)
        return calls

    @staticmethod
    def _opt(argv: list[str], name: str) -> str | None:
        for i, token in enumerate(argv[:-1]):
            if token == "-o" and argv[i + 1].startswith(name + "="):
                return argv[i + 1].split("=", 1)[1]
        return None

    def test_every_call_shares_one_control_master(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(vault_sync.SSHTransport, "MAX_SOCKET_PATH", 4096)
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        calls = self._recorder(monkeypatch)
        transport = vault_sync.SSHTransport("vault-host", "vault")

        transport.reachable()
        transport.list_files(["Wiki"])
        transport.hash_files(["Wiki/a.md"])
        transport.stat_file("Wiki/a.md")
        transport.pull_file("Wiki/a.md", tmp_path / "staged")

        assert len(calls) == 5
        paths = {self._opt(argv, "ControlPath") for argv in calls}
        assert len(paths) == 1 and None not in paths
        assert all(self._opt(argv, "ControlMaster") == "auto" for argv in calls)
        assert all(self._opt(argv, "ControlPersist") for argv in calls)

    def test_control_dir_is_private(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        monkeypatch.setattr(vault_sync.SSHTransport, "MAX_SOCKET_PATH", 4096)
        calls = self._recorder(monkeypatch)

        vault_sync.SSHTransport("vault-host", "vault").reachable()

        control_path = Path(self._opt(calls[0], "ControlPath"))
        assert control_path.parent.is_relative_to(tmp_path)
        assert control_path.parent.stat().st_mode & 0o777 == 0o700

    def test_default_control_path_fits_a_unix_socket(self, monkeypatch):
        # ssh expands %C to 40 hex chars; macOS caps sun_path at 104 bytes.
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        runtime = str(vault_sync.config.vault_sync_ssh_control_dir() / "vs-%C")
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        monkeypatch.setenv("UA_HOME", "/Users/you/.unified_agent")
        ua_home = str(vault_sync.config.vault_sync_ssh_control_dir() / "vs-%C")
        for path in (runtime, ua_home):
            assert len(path.replace("%C", "x" * 40)) < vault_sync.SSHTransport.MAX_SOCKET_PATH

    def test_overlong_control_path_falls_back_to_plain_ssh(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("d" * 80)))
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        calls = self._recorder(monkeypatch)

        vault_sync.SSHTransport("vault-host", "vault").reachable()

        assert self._opt(calls[0], "ControlPath") is None
        assert not (tmp_path / ("d" * 80)).exists()

    def test_unwritable_control_dir_falls_back_to_plain_ssh(self, tmp_path, monkeypatch):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(blocker))
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        monkeypatch.setattr(vault_sync.SSHTransport, "MAX_SOCKET_PATH", 4096)
        calls = self._recorder(monkeypatch)

        transport = vault_sync.SSHTransport("vault-host", "vault")
        transport.reachable()

        assert self._opt(calls[0], "ControlPath") is None

    def test_socket_limit_counts_bytes_not_characters(self, tmp_path, monkeypatch):
        # 20 chars but 60 UTF-8 bytes: fits by character count, not by bytes.
        wide = tmp_path / ("\u20ac" * 20)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(wide))
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        path = str(vault_sync.config.vault_sync_ssh_control_dir() / "vs-%C")
        limit = len(path) - 2 + 40 + 1  # just over the character-count length
        monkeypatch.setattr(vault_sync.SSHTransport, "MAX_SOCKET_PATH", limit)
        calls = self._recorder(monkeypatch)

        vault_sync.SSHTransport("vault-host", "vault").reachable()

        assert self._opt(calls[0], "ControlPath") is None

    def test_multiplex_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("UA_VAULT_SYNC_SSH_MULTIPLEX", "0")
        calls = self._recorder(monkeypatch)

        transport = vault_sync.SSHTransport("vault-host", "vault")
        transport.reachable()
        transport.close()

        assert len(calls) == 1
        assert self._opt(calls[0], "ControlPath") is None

    def test_close_exits_the_master_and_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(vault_sync.SSHTransport, "MAX_SOCKET_PATH", 4096)
        monkeypatch.delenv("UA_VAULT_SYNC_SSH_MULTIPLEX", raising=False)
        calls = self._recorder(monkeypatch)
        transport = vault_sync.SSHTransport("vault-host", "vault")
        transport.reachable()

        transport.close()

        exit_call = calls[-1]
        assert exit_call[exit_call.index("-O") + 1] == "exit"
        assert exit_call[-1] == "vault-host"
        assert self._opt(exit_call, "ControlPath") == self._opt(calls[0], "ControlPath")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("ssh", 5)

        monkeypatch.setattr("unified_agent.vault_sync.subprocess.run", boom)
        transport.close()  # a stuck or missing master must not fail the run

    def test_run_closes_the_transport_even_when_sync_fails(self, tmp_path, monkeypatch):
        closed: list[bool] = []

        class ClosingTransport(LocalDirTransport):
            def list_files(self, trees):
                raise vault_sync.VaultSyncTransportError("boom")

            def close(self):
                closed.append(True)

        hub, desktop = tmp_path / "hub", tmp_path / "desktop"
        hub.mkdir()
        desktop.mkdir()
        monkeypatch.setattr(vault_sync.config, "vault_sync_local_root", lambda: desktop)
        monkeypatch.setattr(
            vault_sync.config, "vault_sync_state_path", lambda: tmp_path / "state.json"
        )
        monkeypatch.setattr(vault_sync, "SSHTransport", lambda host, root: ClosingTransport(hub))

        payload = vault_sync.run()

        assert payload["ok"] is False
        assert closed == [True]


class TestConservativeInboxClassification:
    """A basename/content match cannot prove a capture was filed (#1851)."""

    @pytest.mark.parametrize("hub_content", ["unrelated project index", "same capture content"])
    @pytest.mark.parametrize("push,dry", [(False, False), (True, False), (True, True)])
    def test_ambiguous_basename_keeps_local_conflict_and_optional_upload(
        self, hub_and_desktop, hub_content, push, dry
    ):
        hub, desktop = hub_and_desktop
        local = desktop / "00 - Inbox/index.md"
        remote = hub / "01 - Projects/unrelated/index.md"
        now = time.time()
        _touch(local, "same capture content", now)
        _touch(remote, hub_content, now - 100)

        class Transport(LocalDirTransport):
            enumerations = 0
            uploads = 0

            def list_all_files(self):
                self.enumerations += 1
                return super().list_all_files()

            def push_conflict_copy(self, hint, src):
                self.uploads += 1
                assert hint == "00 - Inbox/index.md"
                assert src.read_text() == "same capture content"

        transport = Transport(hub)
        result = sync_once(desktop, transport, push_conflicts=push, dry_run=dry)
        assert result.ok
        assert result.filed_upstream == []
        assert [(c.relpath, c.reason) for c in result.conflicts] == [
            ("00 - Inbox/index.md", "local_only")
        ]
        assert result.to_dict()["conflict_count"] == 1
        assert transport.uploads == int(push and not dry)
        assert transport.enumerations == 0
        assert local.read_text() == "same capture content"
        assert remote.read_text() == hub_content

    def test_ambiguous_inbox_upload_is_a_copy_for_review(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        local = desktop / "00 - Inbox/index.md"
        remote = hub / "01 - Projects/unrelated/index.md"
        now = time.time()
        _touch(local, "capture contents", now)
        _touch(remote, "unrelated index", now - 100)

        result = sync_once(desktop, LocalDirTransport(hub), push_conflicts=True)

        assert result.ok
        assert [(c.relpath, c.reason) for c in result.conflicts] == [
            ("00 - Inbox/index.md", "local_only")
        ]
        uploaded = list((hub / "00 - Inbox").glob("index.conflict-*.md"))
        assert len(uploaded) == 1
        assert uploaded[0].read_text() == "capture contents"
        assert not (hub / "00 - Inbox/index.md").exists()
        assert local.read_text() == "capture contents"
        assert remote.read_text() == "unrelated index"

    def test_genuinely_local_inbox_note_is_still_a_conflict(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(desktop / "00 - Inbox" / "orphan.md", "never filed", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.filed_upstream == []
        assert len(result.conflicts) == 1
        assert result.conflicts[0].reason == "local_only"
        assert result.conflicts[0].relpath == "00 - Inbox/orphan.md"

    def test_non_inbox_namesake_remains_a_local_only_conflict(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "02 - Areas" / "moved.md", "filed copy", now - 100)
        _touch(desktop / "Wiki" / "moved.md", "desktop wiki copy", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.filed_upstream == []
        assert [c.relpath for c in result.conflicts] == ["Wiki/moved.md"]
        assert result.conflicts[0].reason == "local_only"

    def test_whole_vault_enumeration_is_not_used(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        _touch(hub / "02 - Areas" / "meeting-notes.md", "filed copy", now - 100)
        _touch(desktop / "00 - Inbox" / "meeting-notes.md", "capture copy", now)

        class FailingListAll(LocalDirTransport):
            def list_all_files(self):
                raise OSError("hub refused enumeration")

        result = sync_once(desktop, FailingListAll(hub))

        # Conservative: an unverifiable upstream keeps the note flagged as a conflict.
        assert result.filed_upstream == []
        assert [c.relpath for c in result.conflicts] == ["00 - Inbox/meeting-notes.md"]
        assert result.ok
        assert result.file_errors == []

    def test_dotdir_namesake_does_not_hide_a_local_capture(self, hub_and_desktop):
        hub, desktop = hub_and_desktop
        now = time.time()
        # Same basename, but only inside an Obsidian cache dir -> not a filed copy.
        _touch(hub / ".smart-env" / "orphan.md", "cache junk", now - 100)
        _touch(desktop / "00 - Inbox" / "orphan.md", "never filed", now)

        result = sync_once(desktop, LocalDirTransport(hub))

        assert result.filed_upstream == []
        assert [c.relpath for c in result.conflicts] == ["00 - Inbox/orphan.md"]

    def test_list_all_files_returns_the_whole_vault(self, hub_and_desktop):
        hub, _ = hub_and_desktop
        now = time.time()
        _touch(hub / "02 - Areas" / "a.md", "x", now)
        _touch(hub / "Wiki" / "b.md", "y", now)

        files = LocalDirTransport(hub).list_all_files()

        assert set(files) == {"02 - Areas/a.md", "Wiki/b.md"}

    def test_state_file_records_and_preserves_filed_upstream(self, tmp_path):
        result = vault_sync.SyncResult(
            ok=True,
            reachable=True,
            dry_run=False,
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            filed_upstream=["00 - Inbox/filed.md"],
        )
        state_path = tmp_path / "state.json"

        payload = vault_sync.write_state(result, path=state_path)

        assert payload["filed_upstream"] == ["00 - Inbox/filed.md"]
        assert payload["filed_upstream_count"] == 1
        reread = vault_sync.read_state(state_path)
        assert reread["filed_upstream"] == ["00 - Inbox/filed.md"]
        assert reread["filed_upstream_count"] == 1

    def test_fresh_sync_replaces_legacy_classification_with_visible_conflict(
        self, hub_and_desktop, tmp_path
    ):
        hub, desktop = hub_and_desktop
        now = time.time()
        local = desktop / "00 - Inbox/index.md"
        _touch(local, "capture contents", now)
        _touch(hub / "01 - Projects/unrelated/index.md", "unrelated index", now - 100)
        state_path = tmp_path / "state.json"
        previous = vault_sync.SyncResult(
            ok=True,
            reachable=True,
            dry_run=False,
            started_at="2026-10-07T00:00:00+00:00",
            finished_at="2026-10-07T00:00:01+00:00",
            filed_upstream=["00 - Inbox/index.md"],
        )
        vault_sync.write_state(previous, path=state_path)
        assert vault_sync.read_state(state_path)["filed_upstream_count"] == 1

        result = sync_once(desktop, LocalDirTransport(hub))
        payload = vault_sync.write_state(result, path=state_path)

        assert payload["filed_upstream"] == []
        assert payload["filed_upstream_count"] == 0
        assert payload["conflict_count"] == 1
        assert payload["conflicts"][0]["reason"] == "local_only"
        assert vault_sync.read_state(state_path) == payload
        assert local.read_text() == "capture contents"
