"""Task #2191: per-run Claude dispatch settings files do not accumulate.

Every opted-in Claude dispatch writes ``<run_id>.claude-settings.json`` and a
``<run_id>.ua-dispatch.json`` sidecar under ``data_home()/dispatch-runs``. The
run's end removes both (a refused launch at the spawner, a finished run in the
detached supervisor, the synchronous executor backend), and queue hygiene
expires any left behind after ``UA_DISPATCH_SETTINGS_RETENTION_DAYS``. Launch
logs are evidence and survive both paths. Nothing outside ``dispatch-runs`` is
ever deleted, and symlinks are never followed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from unified_agent import config, queue_hygiene, storage
from unified_agent.dispatch import (
    claude_sandbox,
    coordinator,
    harness_sandbox,
    launch,
    routing,
    supervisor_spawn,
)
from unified_agent.dispatch.launch import (
    CliProcessSpawner,
    RoutedLaunchBoundary,
    StubProcessSpawner,
)
from unified_agent.executor import backends
from unified_agent.tools import delegate, tasks

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process supervision")

DAY = 86400.0


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
    monkeypatch.delenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", raising=False)
    storage.init_db()
    return tmp_path


def _runs_dir() -> Path:
    return config.data_home() / "dispatch-runs"


def _request(tmp_path: Path, task_id: int, run_id: str) -> dict:
    return {
        "binary": "claude",
        "cli_backend": "claude",
        "cwd": str(tmp_path),
        "task_spec": "### Objective\nretention fixture\n",
        "run_id": run_id,
        "task_id": task_id,
        "agent_label": "Desktop_Agent1",
        "env": {},
    }


def _age(path: Path, days: float) -> None:
    then = time.time() - days * DAY
    os.utime(path, (then, then), follow_symlinks=False)


# ── end of run: the spawner's refused launch ────────────────────────────────


def test_launch_refused_by_the_host_probe_removes_settings_and_sidecar(db, tmp_path, monkeypatch):
    task_id = tasks.task_add("refused launch fixture")["id"]
    seen: dict = {}

    def refuse(backend, binary, **kw):
        # The files exist while the probe decides; the refusal then removes them.
        seen["settings"] = Path(kw["dispatch_settings_path"]).is_file()
        raise harness_sandbox.HarnessSandboxUnavailable("harness_sandbox_unavailable:claude")

    monkeypatch.setattr(routing, "headless_argv", refuse)
    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2191-refused"))

    assert out["ok"] is False and out["spawned"] is False
    assert out["error"].startswith("harness_sandbox_unavailable:claude")
    assert seen == {"settings": True}
    assert sorted(p.name for p in _runs_dir().iterdir()) == []


def test_launch_with_missing_binary_removes_settings_and_sidecar(db, tmp_path, monkeypatch):
    task_id = tasks.task_add("missing binary fixture")["id"]
    monkeypatch.setattr(
        routing,
        "headless_argv",
        lambda backend, binary, **kw: [str(tmp_path / "no-such-claude"), "-p", "x"],
    )
    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2191-nobin"))

    assert out["ok"] is False
    assert out["error"].startswith("binary_not_found:")
    assert list(_runs_dir().iterdir()) == []


def test_failed_supervisor_spawn_removes_settings_but_keeps_nothing_else(db, tmp_path, monkeypatch):
    task_id = tasks.task_add("supervisor failure fixture")["id"]
    monkeypatch.setattr(
        routing, "headless_argv", lambda backend, binary, **kw: [sys.executable, "-c", "pass"]
    )

    def boom(*_a, **_kw):
        raise RuntimeError("detached supervisor did not report child pid")

    monkeypatch.setattr("unified_agent.dispatch.supervise.spawn_via_detached_supervisor", boom)
    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2191-supfail"))

    assert out["ok"] is False and out["error"].startswith("spawn_failed:")
    names = {p.name for p in _runs_dir().iterdir()}
    assert not any(n.endswith(claude_sandbox.RUN_ARTIFACT_SUFFIXES) for n in names)


def test_spawned_launch_keeps_its_settings_for_the_run(db, tmp_path, monkeypatch):
    task_id = tasks.task_add("spawned fixture")["id"]
    monkeypatch.setattr(
        routing, "headless_argv", lambda backend, binary, **kw: [sys.executable, "-c", "pass"]
    )
    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        lambda argv, **kw: 4242,
    )
    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2191-live"))

    assert out["spawned"] is True
    assert Path(out["claude_settings_path"]).is_file()
    assert Path(out["dispatch_sidecar_path"]).is_file()


def test_refusal_before_writing_leaves_a_same_named_file_alone(db, tmp_path, monkeypatch):
    """Only files this launch wrote are removed, never another launch's."""
    task_id = tasks.task_add("guard refusal fixture")["id"]
    claude_sandbox.dispatch_runs_dir()
    other = _runs_dir() / f"run-2191-guard{claude_sandbox.SETTINGS_SUFFIX}"
    other.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        launch,
        "_native_guard",
        lambda *_a: {"ok": False, "error": "denied", "spawned": False, "pid": None},
    )
    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2191-guard"))

    assert out["error"] == "denied"
    assert other.is_file()


# ── end of run: the detached supervisor ─────────────────────────────────────


@pytest.fixture
def launched_run(isolated_env, tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    storage.init_db()
    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RoutedLaunchBoundary(spawner=StubProcessSpawner())
    )
    assert out["launched"]
    authority = {
        "data_home": str(config.data_home().resolve()),
        "policy_file": str(policy.resolve()),
        "policy_file_required": False,
        "agent_labels": ":".join(sorted(config.agent_labels())),
        "launch_caller": None,
    }
    # supervisor_spawn.main() writes its parent authority into the process
    # environment; register these keys so monkeypatch restores them and the
    # supervisor tests cannot leak a roster into later tests.
    for key in ("UA_HOME", "UA_POLICY_FILE", "UA_AGENT_LABELS"):
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.setenv(key, "")
            monkeypatch.delenv(key)
    return out["run_id"], authority


@posix_only
def test_supervisor_removes_settings_and_sidecar_when_the_child_exits(
    launched_run, tmp_path, monkeypatch
):
    run_id, authority = launched_run
    settings = claude_sandbox.write_claude_dispatch_settings(run_id, cwd=str(tmp_path))
    sidecar = claude_sandbox.write_dispatch_sidecar(run_id, task_id=1, agent_label="Desktop_Agent1")
    log_path = settings.parent / f"{run_id}.log"
    seen: dict = {}
    real_persist = launch._persist_spawn_pid

    def persist(rid, pid):
        seen["during_run"] = settings.is_file() and sidecar.is_file()
        return real_persist(rid, pid)

    monkeypatch.setattr(launch, "_persist_spawn_pid", persist)
    payload = {
        "argv": [sys.executable, "-c", "print('child ran')"],
        "cwd": str(tmp_path),
        "log_path": str(log_path),
        "run_id": run_id,
        "authority": authority,
    }
    handshake = tmp_path / "handshake"
    handshake.mkdir()
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(handshake / "pid"))

    supervisor_spawn.main()

    assert seen == {"during_run": True}
    assert coordinator.get_run(run_id)["state"] == "finished"
    assert not settings.exists()
    assert not sidecar.exists()
    # The launch log is evidence: the end-of-run path never deletes it.
    assert "child ran" in log_path.read_text(encoding="utf-8")


@posix_only
def test_supervisor_refused_before_start_still_removes_settings(
    launched_run, tmp_path, monkeypatch
):
    run_id, authority = launched_run
    settings = claude_sandbox.write_claude_dispatch_settings(run_id, cwd=str(tmp_path))
    coordinator.cancel_run(run_id, actor="Desktop_Agent5", reason="fixture")
    payload = {
        "argv": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "log_path": str(tmp_path / "child.log"),
        "run_id": run_id,
        "authority": authority,
    }
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(tmp_path / "pid"))

    with pytest.raises(Exception, match="run authority is missing or terminal"):
        supervisor_spawn.main()
    assert not settings.exists()


# ── end of run: the synchronous executor backend ────────────────────────────


def test_executor_backend_removes_settings_after_a_probe_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    harness_sandbox.reset_cache()
    monkeypatch.setattr(
        harness_sandbox,
        "probe_host",
        lambda: harness_sandbox.HostProbe(ok=False, backend="fake-bwrap", detail="probe failed"),
    )
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    try:
        result = backends.ClaudeBackend(claude_bin="claude", skip_permissions=True).run(
            prompt="do the task",
            cwd=".",
            session_dir=".",
            session_id="sess-2191",
            agent_label="fixture-agent",
            timeout_s=5,
        )
    finally:
        harness_sandbox.reset_cache()
    assert result["status"] == "error"
    assert list(_runs_dir().iterdir()) == []


# ── age-based expiry ────────────────────────────────────────────────────────


def _artifact(run_id: str, suffix: str, *, days: float) -> Path:
    path = claude_sandbox.dispatch_runs_dir() / f"{run_id}{suffix}"
    path.write_text("{}", encoding="utf-8")
    _age(path, days)
    return path


def test_expiry_removes_only_stale_settings_and_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.delenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", raising=False)
    s, c = claude_sandbox.SETTINGS_SUFFIX, claude_sandbox.SIDECAR_SUFFIX
    old_settings = _artifact("run-old", s, days=10)
    old_sidecar = _artifact("run-old", c, days=10)
    fresh = _artifact("run-new", s, days=1)
    live = _artifact("run-live", s, days=30)
    old_log = _artifact("run-old", ".log", days=400)
    other = _artifact("notes", ".txt", days=400)

    out = claude_sandbox.expire_stale_run_artifacts(live_run_ids={"run-live"})

    assert out["retention_days"] == 7.0
    assert out["expired"] == sorted([old_settings.name, old_sidecar.name])
    assert not old_settings.exists() and not old_sidecar.exists()
    assert fresh.is_file() and live.is_file()
    # Launch logs are evidence; nothing else in the directory is touched.
    assert old_log.is_file() and other.is_file()


def test_expiry_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    stale = _artifact("run-old", claude_sandbox.SETTINGS_SUFFIX, days=10)
    out = claude_sandbox.expire_stale_run_artifacts(dry_run=True)
    assert out["expired"] == [stale.name] and stale.is_file()


@posix_only
def test_expiry_never_follows_a_symlink_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    _age(outside, 30)
    link = claude_sandbox.dispatch_runs_dir() / f"run-link{claude_sandbox.SETTINGS_SUFFIX}"
    link.symlink_to(outside)
    _age(link, 30)
    nested = claude_sandbox.dispatch_runs_dir() / f"run-dir{claude_sandbox.SETTINGS_SUFFIX}"
    nested.mkdir()
    _age(nested, 30)

    out = claude_sandbox.expire_stale_run_artifacts()

    assert out["expired"] == [link.name]
    assert not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert nested.is_dir()


@posix_only
def test_a_symlinked_dispatch_runs_directory_is_never_cleaned(tmp_path, monkeypatch):
    home = tmp_path / "ua-home"
    home.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    victim = target / f"run-x{claude_sandbox.SETTINGS_SUFFIX}"
    victim.write_text("{}", encoding="utf-8")
    _age(victim, 30)
    (home / "dispatch-runs").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("UA_HOME", str(home))

    assert claude_sandbox.expire_stale_run_artifacts()["expired"] == []
    assert claude_sandbox.snapshot_run_artifacts("run-x") == {}
    forged = {victim: claude_sandbox.artifact_identity(victim)}
    assert claude_sandbox.discard_run_artifacts(forged) == []
    assert victim.is_file()


def test_discard_refuses_paths_outside_dispatch_runs_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    runs = claude_sandbox.dispatch_runs_dir()
    log = runs / "run-a.log"
    log.write_text("evidence", encoding="utf-8")
    outside = tmp_path / f"run-a{claude_sandbox.SETTINGS_SUFFIX}"
    outside.write_text("{}", encoding="utf-8")

    forged = {p: claude_sandbox.artifact_identity(p) for p in (log, outside)}
    assert claude_sandbox.snapshot_artifacts([log, outside]) == {}
    assert claude_sandbox.discard_run_artifacts(forged) == []
    assert claude_sandbox.run_artifact_paths("../run-a") == []
    assert log.is_file() and outside.is_file()


# ── identity, not name: a resumed run reusing the run_id keeps its files ────


def test_a_rewrite_under_the_same_name_is_a_new_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    first = claude_sandbox.write_claude_dispatch_settings("run-r", cwd=str(tmp_path))
    owned = claude_sandbox.snapshot_run_artifacts("run-r")
    again = claude_sandbox.write_claude_dispatch_settings("run-r", cwd=str(tmp_path))
    assert again == first
    assert claude_sandbox.artifact_identity(again) != owned[first]

    assert claude_sandbox.discard_run_artifacts(owned) == []
    assert again.is_file()
    # The new owner's own snapshot still removes it.
    assert claude_sandbox.discard_run_artifacts(claude_sandbox.snapshot_run_artifacts("run-r"))
    assert not again.exists()


@posix_only
def test_lingering_supervisor_keeps_a_resumed_runs_files(launched_run, tmp_path, monkeypatch):
    """Child A self-reports and lingers; a resume rewrites the same names for
    child B; A then exits. A's supervisor must leave B's files alone."""
    run_id, authority = launched_run
    claude_sandbox.write_claude_dispatch_settings(run_id, cwd=str(tmp_path))
    claude_sandbox.write_dispatch_sidecar(run_id, task_id=1, agent_label="Desktop_Agent1")
    resumed: dict = {}
    real_persist = launch._persist_spawn_pid

    def persist_then_resume(rid, pid):
        recorded = real_persist(rid, pid)
        # dispatch_resume relaunches under the same run_id while A still runs.
        resumed["settings"] = claude_sandbox.write_claude_dispatch_settings(
            run_id, cwd=str(tmp_path / "resumed")
        )
        resumed["sidecar"] = claude_sandbox.write_dispatch_sidecar(
            run_id, task_id=2, agent_label="Desktop_Agent1"
        )
        return recorded

    monkeypatch.setattr(launch, "_persist_spawn_pid", persist_then_resume)
    payload = {
        "argv": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "log_path": str(tmp_path / "child.log"),
        "run_id": run_id,
        "authority": authority,
    }
    handshake = tmp_path / "handshake"
    handshake.mkdir()
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(handshake / "pid"))

    supervisor_spawn.main()

    assert resumed["settings"].is_file() and resumed["sidecar"].is_file()
    assert json.loads(resumed["sidecar"].read_text(encoding="utf-8"))["task_id"] == 2
    assert str(tmp_path / "resumed") in resumed["settings"].read_text(encoding="utf-8")


def test_executor_backend_keeps_a_settings_file_rewritten_during_its_run(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)

    def fake_popen(argv, **_kw):
        # Another run reusing this id rewrites the file while this one runs.
        claude_sandbox.write_claude_dispatch_settings("sess-reused", cwd=str(tmp_path))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_popen)
    backends.ClaudeBackend(claude_bin="claude", skip_permissions=True).run(
        prompt="do the task",
        cwd=".",
        session_dir=".",
        session_id="sess-reused",
        agent_label="fixture-agent",
        timeout_s=5,
    )
    assert (_runs_dir() / f"sess-reused{claude_sandbox.SETTINGS_SUFFIX}").is_file()


def test_sweep_keeps_an_entry_rewritten_between_scan_and_unlink(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    stale = _artifact("run-race", claude_sandbox.SETTINGS_SUFFIX, days=30)
    real = claude_sandbox._unlink_if_same

    def resume_races_the_sweep(path, expected):
        claude_sandbox.write_claude_dispatch_settings("run-race", cwd=str(tmp_path))
        return real(path, expected)

    monkeypatch.setattr(claude_sandbox, "_unlink_if_same", resume_races_the_sweep)
    assert claude_sandbox.expire_stale_run_artifacts()["expired"] == []
    assert stale.is_file()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 7.0), ("", 7.0), ("3", 3.0), ("0", 0.0), ("bad", 7.0), ("-1", 7.0), ("nan", 7.0)],
)
def test_retention_days_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", raw)
    assert config.dispatch_settings_retention_days() == expected


def test_zero_retention_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.setenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", "0")
    stale = _artifact("run-old", claude_sandbox.SETTINGS_SUFFIX, days=365)
    assert claude_sandbox.expire_stale_run_artifacts()["expired"] == []
    assert stale.is_file()


def test_hygiene_pass_expires_stale_files_but_keeps_a_live_runs(db, monkeypatch):
    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    run = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RoutedLaunchBoundary(spawner=StubProcessSpawner())
    )
    assert run["launched"]
    live = _artifact(run["run_id"], claude_sandbox.SETTINGS_SUFFIX, days=30)
    stale = _artifact("run-gone", claude_sandbox.SIDECAR_SUFFIX, days=30)
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda _m: False)

    res = queue_hygiene.run_hygiene_pass(send_notifications=False)

    assert res["dispatch_artifacts"]["expired"] == [stale.name]
    assert live.is_file() and not stale.exists()


# ── follow-ups: a write that fails after creating its file (#2196) ──────────


def _replace_then_fail(monkeypatch, *, suffix: str, interloper: bytes | None = None):
    """``os.replace`` that lands the file, then reports failure (as a network FS can).

    With ``interloper``, another writer replaces the entry before the error
    surfaces; the failed writer must then leave that writer's file alone.
    """
    real = os.replace

    def replace(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        if str(dst).endswith(suffix):
            if interloper is not None:
                other = Path(f"{dst}.other")
                other.write_bytes(interloper)
                real(other, dst)
            raise OSError("rename reported failure after completing")

    monkeypatch.setattr(os, "replace", replace)


@pytest.mark.parametrize("suffix", [claude_sandbox.SETTINGS_SUFFIX, claude_sandbox.SIDECAR_SUFFIX])
def test_a_write_that_raises_after_creating_its_file_leaves_nothing(
    db, tmp_path, monkeypatch, suffix
):
    task_id = tasks.task_add("late write failure fixture")["id"]
    monkeypatch.setattr(
        routing, "headless_argv", lambda backend, binary, **kw: [sys.executable, "-c", "pass"]
    )
    _replace_then_fail(monkeypatch, suffix=suffix)

    out = CliProcessSpawner().spawn(_request(tmp_path, task_id, "run-2196-late"))

    assert out["ok"] is False and out["spawned"] is False
    assert "setup_failed:" in out["error"]
    assert sorted(p.name for p in _runs_dir().iterdir()) == []


def test_a_failed_write_keeps_a_file_another_writer_put_in_its_place(tmp_path, monkeypatch):
    """The self-cleanup is identity-checked: it removes only the inode it wrote."""
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    _replace_then_fail(monkeypatch, suffix=claude_sandbox.SETTINGS_SUFFIX, interloper=b"theirs")

    with pytest.raises(OSError, match="after completing"):
        claude_sandbox.write_claude_dispatch_settings("run-2196-race", cwd=str(tmp_path))

    path = _runs_dir() / f"run-2196-race{claude_sandbox.SETTINGS_SUFFIX}"
    assert path.read_bytes() == b"theirs"
    assert sorted(p.name for p in _runs_dir().iterdir()) == [path.name]


# ── follow-ups: crash-orphaned temp files (#2194) ───────────────────────────


def _orphan_tmp(run_id: str, suffix: str, *, days: float, tag: str = "abc123") -> Path:
    path = claude_sandbox.dispatch_runs_dir() / f".{run_id}{suffix}.{tag}.tmp"
    path.write_text("{", encoding="utf-8")
    _age(path, days)
    return path


def test_expiry_removes_stale_orphaned_temp_files(tmp_path, monkeypatch):
    """A crash between mkstemp and os.replace leaves ``.<name>.<rand>.tmp`` (#2194)."""
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.delenv("UA_DISPATCH_SETTINGS_RETENTION_DAYS", raising=False)
    s, c = claude_sandbox.SETTINGS_SUFFIX, claude_sandbox.SIDECAR_SUFFIX
    old_settings_tmp = _orphan_tmp("run-old", s, days=10)
    old_sidecar_tmp = _orphan_tmp("run-old", c, days=10)
    fresh_tmp = _orphan_tmp("run-new", s, days=1)
    live_tmp = _orphan_tmp("run-live", s, days=30)
    unrelated = claude_sandbox.dispatch_runs_dir() / ".run-old.log.abc123.tmp"
    unrelated.write_text("x", encoding="utf-8")
    _age(unrelated, 30)

    dry = claude_sandbox.expire_stale_run_artifacts(live_run_ids={"run-live"}, dry_run=True)
    assert dry["expired"] == sorted([old_settings_tmp.name, old_sidecar_tmp.name])
    assert old_settings_tmp.is_file()

    out = claude_sandbox.expire_stale_run_artifacts(live_run_ids={"run-live"})

    assert out["expired"] == sorted([old_settings_tmp.name, old_sidecar_tmp.name])
    assert not old_settings_tmp.exists() and not old_sidecar_tmp.exists()
    assert fresh_tmp.is_file() and live_tmp.is_file() and unrelated.is_file()


@posix_only
def test_temp_expiry_never_touches_a_symlink_or_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    runs = claude_sandbox.dispatch_runs_dir()
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    _age(outside, 30)
    link = runs / f".run-link{claude_sandbox.SETTINGS_SUFFIX}.abc.tmp"
    link.symlink_to(outside)
    _age(link, 30)
    nested = runs / f".run-dir{claude_sandbox.SETTINGS_SUFFIX}.abc.tmp"
    nested.mkdir()
    _age(nested, 30)

    assert claude_sandbox.expire_stale_run_artifacts()["expired"] == []
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == "keep"
    assert nested.is_dir()
