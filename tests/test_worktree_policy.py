"""Tests for worktree_policy tool."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unified_agent import config, storage
from unified_agent.tools import worktree_policy
from unified_agent.tools.worktree_policy import cleanup_stale_worktrees

# `ps` rows carry the effective uid next to the command line (review round 5).
_UID = os.geteuid()


def test_cleanup_stale_worktrees_dry_run(tmp_path, monkeypatch):
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    worktrees_dir = ws / ".worktrees"
    wt1 = worktrees_dir / "task-101"
    wt1.mkdir(parents=True, exist_ok=True)
    (wt1 / ".git").write_text("gitdir: /repo/.git/worktrees/task-101\n")

    # Insert task 101 as done
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (101, 'Done Task', 'notes', 'done', 'med', '2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    res = cleanup_stale_worktrees(dry_run=True)
    assert res["dry_run"] is True
    assert len(res["stale_worktrees"]) == 1
    assert res["stale_worktrees"][0]["task_id"] == 101
    assert res["stale_worktrees"][0]["reason"] == "task_done"
    assert wt1.exists()


def test_cleanup_stale_worktrees_execution(tmp_path, monkeypatch):
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worktree_policy, "git_worktree_remove", lambda path, force: True)
    # This test is about removal accounting, not the disposability decision;
    # the fixture worktree is a stub directory with no real git behind it, so
    # the real check would (correctly) refuse it and retain it instead.
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (True, ""))

    worktrees_dir = ws / ".worktrees"
    wt2 = worktrees_dir / "task-102"
    wt2.mkdir(parents=True, exist_ok=True)
    (wt2 / ".git").write_text("gitdir: /repo/.git/worktrees/task-102\n")

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (102, 'Blocked Task', 'notes', 'blocked', 'low', '2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    res = cleanup_stale_worktrees(dry_run=False)
    assert res["dry_run"] is False
    assert res["cleaned_count"] == 1


def test_non_worktree_dirs_are_skipped_not_deleted(tmp_path, monkeypatch):
    """`.worktrees/sessions` is executor data, not a worktree — it must survive.

    Regression: every directory under .worktrees/ was treated as a worktree, and
    when `git worktree remove` failed on one (as it must for a plain directory)
    the fallback shutil.rmtree'd it anyway. dry_run defaults to False, so a
    single cleanup call destroyed the Pi executor's RPC session transcripts.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    def _explode(path, force):
        raise RuntimeError(f"'{path}' is not a working tree")

    monkeypatch.setattr(worktree_policy, "git_worktree_remove", _explode)

    sessions = ws / ".worktrees" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    transcript = sessions / "2026-07-25T09-41-13-891Z_task-149-session.jsonl"
    transcript.write_text('{"event": "irreplaceable"}\n')

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert res["stale_worktrees"] == []
    assert res["cleaned_count"] == 0
    assert [s["path"] for s in res["skipped"]] == [".worktrees/sessions"]
    assert transcript.read_text() == '{"event": "irreplaceable"}\n'


def test_nested_worktrees_are_discovered(tmp_path, monkeypatch):
    """A worktree nested inside another must be a cleanup candidate.

    Regression (task #187): the scan only enumerated the top level of
    `.worktrees/`, so `.worktrees/task-180/.worktrees/task-159` — the exact
    shape our own creation bug produced — was invisible to the cleaner. The
    one drift our tooling generates was the one it could not clean.

    The child is listed before its parent: removing the parent first would
    take the nested worktree's files down with it.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    parent = ws / ".worktrees" / "task-180"
    nested = parent / ".worktrees" / "task-159"
    for wt, gitdir in ((parent, "task-180"), (nested, "task-159")):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / ".git").write_text(f"gitdir: /repo/.git/worktrees/{gitdir}\n")

    conn = storage.connect()
    try:
        for tid in (180, 159):
            conn.execute(
                "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
                f"VALUES ({tid}, 'Done Task', 'notes', 'done', 'med', "
                "'2026-07-24T00:00:00', '2026-07-24T00:00:00')"
            )
        conn.commit()
    finally:
        conn.close()

    res = cleanup_stale_worktrees(dry_run=True)

    paths = [c["path"] for c in res["stale_worktrees"]]
    assert ".worktrees/task-180/.worktrees/task-159" in paths
    assert ".worktrees/task-180" in paths
    assert paths.index(".worktrees/task-180/.worktrees/task-159") < paths.index(
        ".worktrees/task-180"
    )


def test_orphaned_worktree_with_unmerged_work_is_retained(tmp_path, monkeypatch):
    """An orphan whose content is not in main is reported, never force-removed.

    `orphaned_task` (a `task-N` worktree with no row N in the DB) used to go
    straight to `git worktree remove --force`, which discards unmerged commits
    and uncommitted files alike. Orphans are produced by ordinary task deletion,
    so this path is reachable with real work sitting in it.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    removed: list[str] = []
    monkeypatch.setattr(
        worktree_policy, "git_worktree_remove", lambda path, force: removed.append(path)
    )
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (False, "dirty"))

    wt = ws / ".worktrees" / "task-184"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/task-184\n")

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert res["cleaned_count"] == 0
    assert [r["path"] for r in res["retained"]] == [".worktrees/task-184"]
    assert res["retained"][0]["reason"] == "orphaned_task"
    assert res["retained"][0]["retained_because"] == "dirty"


def test_orphaned_worktree_that_is_clean_is_removed(tmp_path, monkeypatch):
    """The other half of the decision: clean orphans do get cleaned up.

    Otherwise they accumulate silently — the state task #187 was filed over.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    removed: list[str] = []
    monkeypatch.setattr(
        worktree_policy, "git_worktree_remove", lambda path, force: removed.append(path)
    )
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (True, ""))

    wt = ws / ".worktrees" / "task-183"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/task-183\n")

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == [".worktrees/task-183"]
    assert res["cleaned_count"] == 1
    assert res["retained"] == []


def test_failed_removal_is_reported_not_forced(tmp_path, monkeypatch):
    """A worktree git refuses to remove is reported, and left on disk."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)

    def _explode(path, force):
        raise RuntimeError("contains modified or untracked files")

    monkeypatch.setattr(worktree_policy, "git_worktree_remove", _explode)
    # Force past the disposability gate: this test covers the *other* safety
    # net — what happens when git itself refuses a removal we did approve.
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (True, ""))

    wt = ws / ".worktrees" / "task-103"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/task-103\n")
    precious = wt / "uncommitted.py"
    precious.write_text("# work in progress\n")

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (103, 'Done Task', 'notes', 'done', 'med', "
            "'2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    res = cleanup_stale_worktrees(dry_run=False)

    assert res["cleaned_count"] == 0
    assert [f["path"] for f in res["failed"]] == [".worktrees/task-103"]
    assert precious.read_text() == "# work in progress\n"


# --- Live and deploy checkouts are never stale (task #2174) -----------------
#
# Idleness is judged from the worktree root's mtime, which a running process
# never touches. The hub, node and executor run from a pinned release checkout,
# so after max_idle_days that checkout read as idle_timeout -> stale, and being
# clean and contained in main it also read as disposable: one cleanup call with
# dry_run=False would have force-removed the code of the running hub.


def _stub_worktree(ws, name):
    wt = ws / ".worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: /repo/.git/worktrees/{name}\n")
    return wt


def _removal_recorder(monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(
        worktree_policy, "git_worktree_remove", lambda path, force: removed.append(path)
    )
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (True, ""))
    return removed


def test_release_and_rollback_checkouts_are_kept(monkeypatch):
    """A deploy checkout is kept by name, even idle, clean and merged."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: frozenset())
    _stub_worktree(ws, "release-2161-e727d0d8")
    _stub_worktree(ws, "rollback-2161-0123abcd")

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert res["stale_worktrees"] == []
    assert res["cleaned_count"] == 0


def test_worktree_that_is_a_running_process_cwd_is_kept(tmp_path, monkeypatch):
    """A worktree a live process runs in is in use, whatever its age."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    wt = _stub_worktree(ws, "hub-runtime")
    proc = _proc_running(tmp_path, ["/usr/bin/python3", "-c", "pass"], wt, [])
    monkeypatch.setattr(
        worktree_policy, "_live_process_paths", lambda: worktree_policy._proc_process_paths(proc)
    )

    task_map: dict = {}
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, task_map, cutoff) == (None, False, "in_use")
    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert res["stale_worktrees"] == []


def test_worktree_holding_a_running_process_code_is_kept(monkeypatch):
    """A process launched from a worktree's venv keeps it, though its cwd is elsewhere."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "pinned")
    exe = str(wt.resolve() / ".venv" / "bin" / "unified-agent")
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: frozenset({exe}))

    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff) == (None, False, "in_use")


def test_unknown_process_state_fails_safe_to_in_use(monkeypatch):
    """If live processes cannot be listed, nothing is called idle."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: None)
    _stub_worktree(ws, "unknown-state")

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert res["stale_worktrees"] == []


def test_ordinary_idle_merged_worktree_is_still_removed(monkeypatch):
    """The guard is narrow: an idle, clean, merged worktree nobody uses still goes."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    sibling = _stub_worktree(ws, "idle-feature")
    # A process inside a worktree whose name only shares a prefix does not
    # protect this one.
    other = str(sibling.resolve()) + "-other"
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: frozenset({other}))

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert [c["reason"] for c in res["stale_worktrees"]] == ["idle_timeout"]
    assert removed == [".worktrees/idle-feature"]
    assert res["cleaned_count"] == 1


def test_checkout_that_comes_into_use_before_removal_is_retained(monkeypatch):
    """Liveness is read again just before each forced removal."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    wt = _stub_worktree(ws, "late-user")
    snapshots = iter([frozenset(), frozenset({str(wt.resolve())})])
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: next(snapshots))

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert res["cleaned_count"] == 0
    assert [(r["path"], r["retained_because"]) for r in res["retained"]] == [
        (".worktrees/late-user", "in_use")
    ]


def test_recheck_scan_failure_is_reported_as_liveness_unknown(monkeypatch):
    """A pre-removal recheck that cannot list processes says so, with its cause (#2196)."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    _stub_worktree(ws, "recheck-unknown")
    cause = "a process with the hub's own credentials refused a read: pid 5151 (agentd)"
    calls = iter([(frozenset(), None), (None, cause)])

    def _scan():
        paths, why = next(calls)
        worktree_policy._last_unknown_cause = why
        return paths

    monkeypatch.setattr(worktree_policy, "_last_unknown_cause", None)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", _scan)

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)

    assert removed == []
    assert [(r["path"], r["retained_because"]) for r in res["retained"]] == [
        (".worktrees/recheck-unknown", "liveness_unknown")
    ]
    assert res["liveness_unknown"] is True
    assert "pid 5151 (agentd)" in res["note"]


def test_initial_scan_cause_survives_a_later_successful_recheck(monkeypatch):
    """The note names the first scan's cause even if a later recheck succeeded."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    _removal_recorder(monkeypatch)
    _stub_worktree(ws, "task-781")
    _insert_task(781, "done")
    cause = "no /proc, and lsof could not run"
    calls = iter([(None, cause), (frozenset(), None)])

    def _scan():
        paths, why = next(calls)
        worktree_policy._last_unknown_cause = why
        return paths

    monkeypatch.setattr(worktree_policy, "_last_unknown_cause", None)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", _scan)

    res = cleanup_stale_worktrees(dry_run=False)

    assert res["liveness_unknown"] is True
    assert cause in res["note"]


def test_checkout_on_a_running_process_pythonpath_is_seen(tmp_path):
    """`python -m` with PYTHONPATH into a checkout, cwd elsewhere, is in use."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _proc_running(
        tmp_path, ["/usr/bin/python3", "-m", "tool"], elsewhere, [f"PYTHONPATH={checkout}"]
    )
    paths = worktree_policy._proc_process_paths(proc)
    assert worktree_policy._in_use(checkout, paths)


def test_env_and_ps_code_paths(monkeypatch):
    """PYTHONPATH and VIRTUAL_ENV entries and absolute argv tokens are collected."""
    import subprocess

    assert worktree_policy._env_code_paths(
        ["PYTHONPATH=/a/src:rel:/b", "VIRTUAL_ENV=/c/.venv", "HOME=/home/x", "noise"]
    ) == {"/a/src", "/b", "/c/.venv"}

    out = f"  41 {_UID} python3 /wt/run.py --flag PYTHONPATH=/wt2/src HOME=/h\n 42 {_UID} /usr/bin/zsh\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert {"/wt/run.py", "/wt2/src"} <= worktree_policy._ps_process_paths()

    def _missing(*a, **k):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", _missing)
    assert worktree_policy._ps_process_paths() == set()


def test_doctor_reports_kept_live_worktrees_not_removable(monkeypatch):
    """The doctor row counts kept checkouts and never offers them for removal."""
    from unified_agent.tools import doctor

    found = [
        {
            "path": "/x/.worktrees/release-2161-e727d0d8",
            "in_workspace": False,
            "disposable": True,
            "stale": False,
            "stale_reason": "deploy_checkout",
        },
        {
            "path": "/x/.worktrees/hub-runtime",
            "in_workspace": False,
            "disposable": True,
            "stale": False,
            "stale_reason": "in_use",
        },
    ]
    monkeypatch.setattr(
        worktree_policy, "cached_survey_worktrees", lambda force=False: (found, 0.0)
    )

    row = doctor._stale_worktrees_check()

    assert row["status"] == "LIVE"
    assert "none stale" in row["detail"]
    assert "2 kept" in row["detail"]
    assert "can be removed" not in row["detail"]


# --- Idleness from git metadata (task #2177) ---------------------------------
#
# The root directory's mtime changes only when an entry directly inside it is
# created, removed or renamed. Editing files in subdirectories, committing and
# checking out leave it alone, so an actively worked worktree with no live
# process read as idle_timeout after max_idle_days.


def _git_run(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _real_worktree(tmp_path, name="feature-x"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(repo, "init", "-q", "-b", "main")
    _git_run(repo, "config", "user.name", "Test")
    _git_run(repo, "config", "user.email", "test@example.invalid")
    _git_run(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "base")
    wt = repo / ".worktrees" / name
    wt.parent.mkdir()
    _git_run(repo, "worktree", "add", "-q", "-b", name, str(wt))
    return repo, wt


def _gitdir(wt: Path) -> Path:
    raw = (wt / ".git").read_text().strip()
    return Path(raw.removeprefix("gitdir:").strip())


def _age(path: Path, days: float) -> None:
    import os
    import time

    then = time.time() - days * 86400
    os.utime(path, (then, then), follow_symlinks=False)


def _age_worktree(wt: Path, days: float) -> None:
    gitdir = _gitdir(wt)
    for p in (wt, *(gitdir / name for name in worktree_policy._ACTIVITY_FILES)):
        if p.exists():
            _age(p, days)


def test_recent_commit_in_a_worktree_with_an_old_root_is_not_idle(tmp_path):
    _, wt = _real_worktree(tmp_path)
    (wt / "sub").mkdir()
    (wt / "sub" / "work.py").write_text("x = 1\n")
    _git_run(wt, "add", "-A")
    _git_run(wt, "commit", "-q", "-m", "work")
    _age(wt, 30)

    cutoff = datetime.now(UTC) - timedelta(days=7)
    verdict = worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset())

    assert verdict == (None, False, None)


def test_worktree_with_all_git_metadata_old_is_idle(tmp_path):
    _, wt = _real_worktree(tmp_path)
    _age_worktree(wt, 30)

    cutoff = datetime.now(UTC) - timedelta(days=7)
    verdict = worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset())

    assert verdict == (None, True, "idle_timeout")


def test_survey_does_not_change_the_idleness_verdict(tmp_path, monkeypatch):
    """The survey's own `git status` must not refresh the index it judges by."""
    repo, wt = _real_worktree(tmp_path)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: frozenset())
    # Stat-dirty but content-clean: a plain `git status` would rewrite the
    # index to record the new stat data, freshening its mtime.
    _age(wt / "a.txt", 20)
    _age_worktree(wt, 30)
    index = _gitdir(wt) / "index"
    before = index.stat().st_mtime

    first = worktree_policy.survey_worktrees(extra_roots=[repo], max_idle_days=7)
    second = worktree_policy.survey_worktrees(extra_roots=[repo], max_idle_days=7)

    for found in (first, second):
        (entry,) = [f for f in found if f["path"].endswith(".worktrees/feature-x")]
        assert (entry["stale"], entry["stale_reason"]) == (True, "idle_timeout")
    assert index.stat().st_mtime == before


# --- In-use detection follow-ups (task #2178) --------------------------------


def test_relative_argv_is_resolved_against_cwd_and_path(tmp_path):
    """A PATH-resolved console script and a cwd-relative script both name the checkout."""
    import os

    bindir = tmp_path / "wt" / ".venv" / "bin"
    bindir.mkdir(parents=True)
    script = bindir / "unified-agent"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    paths = worktree_policy._argv_paths(
        ["unified-agent", "serve", "../wt/run.py", "--flag=x", "origin/main", "HOME=/h"],
        cwd=str(tmp_path / "elsewhere"),
        path_env=f"/nonexistent{os.pathsep}{bindir}",
    )

    assert str(script) in paths
    assert str(tmp_path / "wt" / "run.py") in paths


def test_checkout_run_by_relative_argv_from_elsewhere_is_seen(tmp_path):
    """`python ../checkout/x` from a sibling dir, and a PATH-found entry point, are in use."""
    import sys

    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    (checkout / "bin" / "pysleep").symlink_to(sys.executable)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    relative = _proc_running(
        tmp_path / "relative", ["/usr/bin/python3", "-c", "pass", "../checkout/x"], elsewhere, []
    )
    on_path = _proc_running(
        tmp_path / "on-path", ["pysleep", "-c", "pass"], elsewhere, [f"PATH={checkout / 'bin'}"]
    )
    for proc, expected in ((relative, checkout / "x"), (on_path, checkout / "bin" / "pysleep")):
        paths = worktree_policy._proc_process_paths(proc)
        assert paths is not None
        assert str(expected) in paths


@pytest.mark.live_process_scan
@pytest.mark.skipif(
    os.environ.get("UA_TEST_REAL_PROC") != "1" or not Path("/proc/self/environ").exists(),
    reason="reads the host's real /proc; set UA_TEST_REAL_PROC=1 on Linux to run (task #2281)",
)
def test_real_process_scan_sees_live_checkouts(tmp_path, monkeypatch):
    """Integration: the host's own scan sees a cwd, PYTHONPATH, relative argv and PATH lookup.

    Opt-in because the answer depends on the host: one same-user process that
    refuses a read (a non-dumpable ssh-agent) makes the real scan honestly
    unknown, which says nothing about this code. Every other test gets an
    empty or look-alike process table from conftest.
    """
    import subprocess
    import sys

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    wt = _stub_worktree(ws, "hub-runtime")
    pythonpath = tmp_path / "pythonpath"
    pythonpath.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    (checkout / "bin" / "pysleep").symlink_to(sys.executable)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sleep = "import time; time.sleep(60)"
    procs = [
        subprocess.Popen([sys.executable, "-c", sleep], cwd=wt),
        subprocess.Popen(
            [sys.executable, "-c", sleep],
            cwd=elsewhere,
            env={**os.environ, "PYTHONPATH": str(pythonpath)},
        ),
        subprocess.Popen([sys.executable, "-c", sleep, "../checkout/x"], cwd=elsewhere),
        subprocess.Popen(
            ["pysleep", "-c", sleep],
            cwd=elsewhere,
            env={**os.environ, "PATH": str(checkout / "bin")},
        ),
    ]
    try:
        paths = worktree_policy._live_process_paths()
        assert paths is not None, worktree_policy._last_unknown_cause
        res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()

    assert worktree_policy._in_use(wt, paths)
    assert worktree_policy._in_use(pythonpath, paths)
    assert str(checkout / "x") in paths
    assert str(checkout / "bin" / "pysleep") in paths
    assert removed == []
    assert res["stale_worktrees"] == []


def test_ps_relative_argv_uses_lsof_cwd(monkeypatch):
    """Without /proc, lsof's per-process cwd resolves relative argv from ps."""
    import subprocess

    lsof_out = "p123\nfcwd\nn/a/b\nftxt\nn/usr/bin/python3\np124\nfcwd\nn/\n"
    ps_out = f"123 {_UID} python3 ../wt/run.py PATH=/usr/bin\n124 {_UID} /usr/bin/zsh\n"

    def _run(cmd, *a, **k):
        out = lsof_out if cmd[0] == "lsof" else ps_out
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    paths = worktree_policy._lsof_process_paths()

    assert paths is not None
    assert {"/a/b", "/usr/bin/python3", "/a/wt/run.py"} <= paths


def _fake_proc(tmp_path, *, own_status: str, procs: dict[str, str]) -> Path:
    """A /proc look-alike: `self/status` plus per-pid status, cwd, cmdline."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "status").write_text(own_status)
    for pid, status in procs.items():
        entry = proc / pid
        entry.mkdir()
        (entry / "status").write_text(status)
        (entry / "cmdline").write_bytes(b"/usr/bin/thing\0")
        (entry / "environ").write_bytes(b"")
        (entry / "cwd").symlink_to(tmp_path)
        (entry / "exe").symlink_to("/usr/bin/thing")
        (entry / "comm").write_text(f"daemon{pid}\n")
    return proc


_OWN = "Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1000\t1000\t1000\nGroups:\t1000\nCapPrm:\t0\n"
_ELEVATED = (
    "Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1001\t1001\t1001\nGroups:\t1000\nCapPrm:\t0\n"
)
_OTHER = "Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\nGroups:\t0\nCapPrm:\t1ff\n"


def _deny(monkeypatch, pids):
    real = worktree_policy._readlink

    def _readlink(path):
        if Path(path).parent.name in pids:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(worktree_policy, "_readlink", _readlink)


def test_permission_denied_on_a_same_credential_process_fails_safe(tmp_path, monkeypatch):
    """A process with the scanner's own credentials should be readable; if not, unknown."""
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN, "11": _OWN})
    _deny(monkeypatch, {"11"})

    assert worktree_policy._proc_process_paths(proc) is None
    assert worktree_policy._scan_proc(proc) == (
        None,
        "a process with the hub's own credentials refused a read: pid 11 (daemon11)",
    )


def test_denied_process_names_are_bounded_and_sanitized(tmp_path, monkeypatch):
    """The reason names at most three refusing processes, with printable names."""
    pids = ["11", "12", "13", "14", "15"]
    proc = _fake_proc(tmp_path, own_status=_OWN, procs=dict.fromkeys(pids, _OWN))
    (proc / "11" / "comm").write_bytes(b"evil\x1b[31m name" + b"x" * 100 + b"\n")
    _deny(monkeypatch, set(pids))

    paths, cause = worktree_policy._scan_proc(proc)

    assert paths is None
    assert "\x1b" not in cause and " name" not in cause
    assert cause.count("pid ") == 3
    assert cause.endswith("and 2 more")
    first = cause.split(": ", 1)[1].split(", ")[0]
    assert first.startswith("pid 11 (evil??31m?name") and len(first) <= len("pid 11 ()") + 32


def test_liveness_cause_reaches_cleanup_note_and_doctor(monkeypatch):
    """An operator sees which process blocks cleanup, not just that it is blocked."""
    from unified_agent.tools import doctor

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    _removal_recorder(monkeypatch)
    _stub_worktree(ws, "task-779")
    _insert_task(779, "done")
    cause = "a process with the hub's own credentials refused a read: pid 4242 (keyringd)"

    def _unknown():
        worktree_policy._last_unknown_cause = cause
        return None

    monkeypatch.setattr(worktree_policy, "_last_unknown_cause", None)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", _unknown)

    res = cleanup_stale_worktrees(dry_run=False)
    assert "pid 4242 (keyringd)" in res["note"]

    found = worktree_policy.survey_worktrees()
    monkeypatch.setattr(
        worktree_policy, "cached_survey_worktrees", lambda force=False: (found, 0.0)
    )
    row = doctor._stale_worktrees_check()
    assert row["status"] == "DEGRADED"
    assert "pid 4242 (keyringd)" in row["detail"]


def test_permission_denied_on_other_or_elevated_processes_is_skipped(tmp_path, monkeypatch):
    """Root-owned and setgid/capability processes are always EACCES; they are skipped."""
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN, "11": _OTHER, "12": _ELEVATED})
    _deny(monkeypatch, {"11", "12"})

    paths = worktree_policy._proc_process_paths(proc)

    assert paths is not None
    assert str(tmp_path) in paths
    # The world-readable cmdline of a skipped process is still used.
    assert "/usr/bin/thing" in paths


def test_unlistable_proc_is_unknown(tmp_path):
    """An OSError listing /proc yields unknown, not an empty process table."""
    assert worktree_policy._proc_process_paths(tmp_path / "missing") is None


def test_worktree_removed_mid_scan_is_not_idle(tmp_path):
    """A root that vanished between listing and stat reads as active, not as an error."""
    gone = tmp_path / ".worktrees" / "task-9001"
    cutoff = datetime.now(UTC) - timedelta(days=7)

    assert worktree_policy._worktree_staleness(gone, {}, cutoff, frozenset()) == (
        9001,
        True,
        "orphaned_task",
    )
    assert worktree_policy._worktree_staleness(gone.with_name("x"), {}, cutoff, frozenset()) == (
        None,
        False,
        None,
    )


def test_recent_fetch_counts_as_activity(tmp_path):
    """A worktree touched only by `git fetch` (FETCH_HEAD) is not idle."""
    _, wt = _real_worktree(tmp_path)
    _age_worktree(wt, 30)
    (_gitdir(wt) / "FETCH_HEAD").write_text("0" * 40 + "\t\tbranch 'main'\n")

    cutoff = datetime.now(UTC) - timedelta(days=7)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset()) == (None, False, None)


@pytest.mark.parametrize(
    ("name", "deploy"),
    [
        ("release-97d0dca6", True),
        ("release-2161-e727d0d8", True),
        ("release-20261015-seven", True),
        ("release-2158-2159-20261018", True),
        ("rollback-2161-0123abcd", True),
        # Over-matching on purpose: deploy names have no fixed grammar, and
        # keeping an ordinary worktree is the safe side.
        ("release-notes", True),
        ("rollback-hotfix", True),
        ("release-v2", True),
        ("task-12-release-1", False),
        ("feature-release", False),
    ],
)
def test_deploy_checkout_names(name, deploy):
    assert worktree_policy._is_deploy_checkout(name) is deploy


def _insert_task(tid: int, status: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            f"VALUES ({tid}, 'T', 'notes', '{status}', 'med', "
            "'2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()


def test_in_use_worktree_of_a_done_task_is_reported_never_removed(monkeypatch):
    """A parked shell must not hide a done task's worktree, nor let it be removed."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    wt = _stub_worktree(ws, "task-777")
    _insert_task(777, "done")
    monkeypatch.setattr(
        worktree_policy, "_live_process_paths", lambda: frozenset({str(wt.resolve())})
    )

    dry = cleanup_stale_worktrees(dry_run=True)
    assert [(c["path"], c["reason"], c["in_use"]) for c in dry["stale_worktrees"]] == [
        (".worktrees/task-777", "task_done", True)
    ]

    res = cleanup_stale_worktrees(dry_run=False)
    assert removed == []
    assert res["cleaned_count"] == 0
    assert [(r["path"], r["retained_because"]) for r in res["retained"]] == [
        (".worktrees/task-777", "in_use")
    ]

    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda path: (True, ""))
    (entry,) = [e for e in worktree_policy.survey_worktrees() if e["path"].endswith("task-777")]
    assert (entry["stale"], entry["stale_reason"], entry["in_use"]) == (True, "task_done", True)


def test_in_use_idle_worktree_is_still_not_stale(monkeypatch):
    """Live use contradicts idleness, so an idle-only verdict stays hidden as in_use."""
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "service-checkout")
    live = frozenset({str(wt.resolve())})
    cutoff = datetime.now(UTC) + timedelta(days=1)

    assert worktree_policy._worktree_staleness(wt, {}, cutoff, live) == (None, False, "in_use")


def test_unknown_liveness_is_surfaced_not_silent(monkeypatch):
    """Without a way to list processes cleanup removes nothing, and says so."""
    from unified_agent.tools import doctor

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: None)
    _stub_worktree(ws, "task-778")
    _insert_task(778, "done")
    _stub_worktree(ws, "idle-thing")

    res = cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []
    assert res["liveness_unknown"] is True
    assert "removes nothing" in res["note"]
    assert [(r["path"], r["retained_because"]) for r in res["retained"]] == [
        (".worktrees/task-778", "liveness_unknown")
    ]

    found = worktree_policy.survey_worktrees(max_idle_days=0.0)
    by_name = {Path(e["path"]).name: e for e in found}
    assert by_name["idle-thing"]["stale_reason"] == "liveness_unknown"
    assert by_name["idle-thing"]["in_use"] is None
    assert by_name["task-778"]["in_use"] is None

    monkeypatch.setattr(
        worktree_policy, "cached_survey_worktrees", lambda force=False: (found, 0.0)
    )
    row = doctor._stale_worktrees_check()
    assert row["status"] == "DEGRADED"
    assert "liveness" in row["detail"]


def test_doctor_does_not_offer_an_in_use_stale_worktree_for_removal(monkeypatch):
    from unified_agent.tools import doctor

    found = [
        {
            "path": "/x/.worktrees/task-777",
            "in_workspace": True,
            "disposable": True,
            "stale": True,
            "stale_reason": "task_done",
            "in_use": True,
        }
    ]
    monkeypatch.setattr(
        worktree_policy, "cached_survey_worktrees", lambda force=False: (found, 0.0)
    )

    row = doctor._stale_worktrees_check()

    assert row["status"] == "DORMANT"
    assert "0 hold nothing" in row["detail"]
    assert "1 in use" in row["detail"]


# --- In-use detection follow-ups (task #2192) --------------------------------


def _tool_on_path(tmp_path, name="tool"):
    bindir = tmp_path / "wt" / ".venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / name
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return bindir, str(script)


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "X=1", "-u", "HOME", "tool", "serve"],
        ["/usr/bin/env", "-i", "--", "tool"],
        ["env", "-", "tool"],
        ["sudo", "-u", "someone", "-E", "X=1", "tool"],
        ["doas", "-u", "someone", "tool"],
        ["nice", "-n", "5", "tool"],
        ["nice", "-10", "tool"],
        ["nohup", "setsid", "-f", "tool"],
        ["timeout", "-s", "KILL", "30", "tool"],
        ["timeout", "--kill-after=5", "30s", "env", "A=b", "tool"],
        ["bash", "-c", "tool --flag"],
        ["bash", "-lc", "cd /srv && X=1 exec tool"],
        ["/bin/sh", "-e", "-c", "echo hi; nohup tool &"],
        ["zsh", "-o", "pipefail", "-c", "tool | cat"],
        ["dash", "-c", "sh -c 'env Y=2 tool'"],
        # Unbalanced quote: falls back to a plain split, still finds it.
        ["bash", "-c", "tool 'unterminated"],
    ],
)
def test_bare_program_behind_wrappers_and_shells_is_resolved(tmp_path, argv):
    """env, sudo, nice, timeout ... and `sh -c '<cmd>'` do not hide the program (#2192)."""
    bindir, script = _tool_on_path(tmp_path)

    paths = worktree_policy._argv_paths(argv, cwd=None, path_env=str(bindir))

    assert script in paths


def test_ps_line_finds_a_bare_program_inside_a_split_shell_string(tmp_path, monkeypatch):
    """`ps` joins argv unquoted, so a `-c` string arrives in pieces (#2192).

    `bash -c 'while :; do tool; done'` reads back as `bash -c while :; do
    tool; done`; the whole line is read as a command string too, so `tool`
    is still looked up on PATH.
    """
    import subprocess

    bindir, script = _tool_on_path(tmp_path)
    out = f" 77 {_UID} bash -c while :; do tool; sleep 60; done PATH={bindir}\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )

    assert script in worktree_policy._ps_process_paths()


def test_shell_command_paths_are_collected(tmp_path):
    """Absolute and cwd-relative paths inside a `-c` string name checkouts too."""
    paths = worktree_policy._argv_paths(
        ["bash", "-c", "cd /srv/wt && python ../wt2/run.py"],
        cwd=str(tmp_path),
        path_env="/nonexistent",
    )

    assert "/srv/wt" in paths
    assert str(tmp_path.parent / "wt2" / "run.py") in paths


def test_wrapper_and_shell_parsing_is_bounded(tmp_path):
    """Deep `sh -c` nesting and huge strings are cut off, never recursed without end."""
    bindir, script = _tool_on_path(tmp_path)
    nested = "tool"
    for _ in range(10):
        nested = f"sh -c {nested!r}"
    worktree_policy._argv_paths(["sh", "-c", nested], cwd=None, path_env=str(bindir))

    huge = "x " * 100_000 + "tool"
    worktree_policy._argv_paths(["bash", "-c", huge], cwd=None, path_env=str(bindir))
    assert worktree_policy._argv_paths(["env"] * 50, cwd=None, path_env=str(bindir)) == set()


def test_unreadable_process_path_falls_back_to_the_hub_path(tmp_path, monkeypatch):
    """No PATH for the process (environ unreadable): the hub's own PATH resolves it."""
    bindir, script = _tool_on_path(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))

    assert script in worktree_policy._argv_paths(["tool"], cwd=None, path_env=None)

    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OTHER})
    (proc / "10" / "cmdline").write_bytes(b"tool\0serve\0")
    _deny(monkeypatch, {"10"})

    paths = worktree_policy._proc_process_paths(proc)

    assert paths is not None and script in paths


def test_path_lookups_are_cached_within_one_scan(tmp_path, monkeypatch):
    """Many processes running the same program cost one PATH search per scan."""
    import shutil

    bindir, script = _tool_on_path(tmp_path)
    proc = _fake_proc(tmp_path, own_status=_OWN, procs=dict.fromkeys(["10", "11", "12"], _OWN))
    for pid in ("10", "11", "12"):
        (proc / pid / "cmdline").write_bytes(b"tool\0")
        (proc / pid / "environ").write_bytes(f"PATH={bindir}\0".encode())
    calls = []
    real = shutil.which

    def _which(name, *a, **k):
        calls.append(name)
        return real(name, *a, **k)

    monkeypatch.setattr(worktree_policy.shutil, "which", _which)

    assert script in worktree_policy._proc_process_paths(proc)
    assert calls == ["tool"]
    # Per scan, not global: a second scan looks again.
    worktree_policy._proc_process_paths(proc)
    assert calls == ["tool", "tool"]


_GROUPS_ONLY = "Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1000\t1000\t1000\nGroups:\nCapPrm:\t0\n"


def test_refusing_process_that_differs_only_in_groups_is_skipped(tmp_path, monkeypatch):
    """Kept as other-credential on purpose (#2192): see `_scan_proc`.

    systemd's `(sd-pam)` helper drops its supplementary groups without an exec,
    so it is non-dumpable and refuses every read, on every user session.
    Counting it as same-credential would leave liveness unknown permanently.
    """
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN, "11": _GROUPS_ONLY})
    _deny(monkeypatch, {"11"})

    paths, cause = worktree_policy._scan_proc(proc)

    assert cause is None and paths is not None


def test_relative_gitdir_file_resolves_against_the_worktree(tmp_path):
    """A `.git` file with `gitdir: ../x` points beside the worktree, not at the cwd."""
    import os

    wt = tmp_path / "wt"
    wt.mkdir()
    gitdir = tmp_path / "x"
    gitdir.mkdir()
    (wt / ".git").write_text("gitdir: ../x\n")
    (gitdir / "index").write_text("")
    _age(wt, 30)
    _age(gitdir / "index", 1)

    assert worktree_policy._git_dir(wt).resolve() == gitdir.resolve()
    activity = worktree_policy._last_activity(wt)
    assert abs(activity.timestamp() - os.stat(gitdir / "index").st_mtime) < 1


# --- Review of PR #1293: shell keywords, env -S, bounds (task #2192) --------


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "while true; do tool; sleep 1; done"],
        ["sh", "-c", "until false; do tool; done"],
        ["bash", "-c", "if x; then tool; fi"],
        ["bash", "-c", "if x; then :; elif y; then :; else tool; fi"],
        ["bash", "-c", "time tool"],
        ["bash", "-c", "builtin tool"],
        ["bash", "-c", "{ tool; }"],
        ["bash", "-c", "for f in a; do tool; done"],
        ["bash", "-c", "select x in a; do tool; done"],
        ["bash", "-c", "! tool"],
        ["bash", "-c", "coproc tool"],
        ["bash", "-c", "function f { tool; }; f"],
        ["bash", "-c", "case x in x) tool;; esac"],
        # Not at a command position at all: the bare-word backstop.
        ["bash", "-c", "somerunner --every 5 tool"],
        # env -S splits its string like a shell would.
        ["env", "-S", "tool --flag", "/x/script"],
        ["/usr/bin/env", "-S tool -u", "/x/script"],
        ["env", "-Stool"],
        ["env", "--split-string=X=1 tool -u"],
        ["env", "--split-string", "tool"],
        # `--` ends options, not assignments.
        ["env", "--", "X=1", "tool"],
        ["sudo", "--", "X=1", "tool"],
        # GNU timeout permutes: options may follow the duration.
        ["timeout", "30", "-k", "5", "tool"],
        # Backticks are command substitution, like $(...).
        ["bash", "-c", "echo `tool`"],
        ["bash", "-c", "echo $(tool)"],
        ["ksh", "-c", "tool"],
        ["mksh", "-c", "tool"],
        ["fish", "-c", "tool"],
        ["xargs", "-n", "1", "-P", "4", "tool"],
        ["xargs", "-a", "list.txt", "-I{}", "tool", "{}"],
        ["xargs", "--max-procs=4", "-d", "\\n", "tool"],
        ["find", ".", "-name", "x", "-exec", "tool", "{}", ";"],
        ["find", ".", "-execdir", "sh", "-c", "tool", ";"],
        ["find", ".", "-ok", "env", "X=1", "tool", "{}", "+"],
    ],
)
def test_program_inside_shell_syntax_is_resolved(tmp_path, argv):
    """Keywords, env -S, xargs and find -exec do not hide the program (#2192 review)."""
    bindir, script = _tool_on_path(tmp_path)

    paths = worktree_policy._argv_paths(argv, cwd=None, path_env=str(bindir))

    assert script in paths


def test_absolute_program_in_a_shell_loop_is_seen():
    """`sh -c 'while :; do <checkout>/tool; sleep 60; done'` names the checkout."""
    paths = worktree_policy._argv_paths(
        ["sh", "-c", "while :; do /wt/.venv/bin/tool sync; sleep 60; done"],
        cwd=None,
        path_env="/nonexistent",
    )
    assert "/wt/.venv/bin/tool" in paths


@pytest.mark.parametrize(
    "command",
    [
        "echo " + " ".join(f"w{i}" for i in range(600)) + " tool",
        "echo " + " ".join(f"w{i}" for i in range(600)) + "; tool",
        "echo '" + "x" * 20_000 + "'; tool",
    ],
    ids=["argument-past-600-words", "command-past-600-words", "past-the-shlex-length"],
)
def test_program_past_the_token_cap_is_found(tmp_path, command):
    """A long `-c` string is scanned whole, not cut at a token count."""
    bindir, script = _tool_on_path(tmp_path)

    paths = worktree_policy._argv_paths(["bash", "-c", command], cwd=None, path_env=str(bindir))

    assert script in paths


def test_a_checkout_binary_named_like_a_wrapper_is_resolved(tmp_path):
    """argv[0] itself is resolved too, even when it is a wrapper name."""
    bindir, script = _tool_on_path(tmp_path, name="nice")

    paths = worktree_policy._argv_paths(
        ["nice", "-n", "5", "sleep", "60"], cwd=None, path_env=str(bindir)
    )

    assert script in paths


# --- In-use detection follow-ups (task #2199) --------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "PATH={bin}", "tool"],
        ["/usr/bin/env", "-i", "PATH={bin}", "tool", "serve"],
        ["sudo", "PATH={bin}", "tool"],
        ["sudo", "-u", "someone", "--", "PATH={bin}:$PATH", "tool"],
        ["sh", "-c", "env PATH={bin} tool"],
        ["sh", "-c", "PATH={bin} tool --serve"],
        ["bash", "-c", "cd /srv && PATH={bin}:$PATH exec tool"],
        ["bash", "-c", "export PATH={bin}:$PATH; tool"],
        # The override is inherited by the shell the wrapper runs.
        ["env", "PATH={bin}", "sh", "-c", "tool"],
        ["env", "PATH={bin}", "find", ".", "-exec", "tool", "{}", ";"],
    ],
)
def test_a_path_override_in_a_wrapper_resolves_the_program(tmp_path, argv):
    """`env PATH=<wt>/bin tool` runs <wt>/bin/tool, whatever the process PATH (#2199)."""
    bindir, script = _tool_on_path(tmp_path)
    argv = [arg.replace("{bin}", str(bindir)) for arg in argv]

    paths = worktree_policy._argv_paths(argv, cwd=None, path_env="/nonexistent/bin")

    assert script in paths


def test_a_path_override_still_looks_on_the_original_path(tmp_path):
    """The override is a union, not a replacement: over-keeping is the safe side."""
    bindir, script = _tool_on_path(tmp_path)

    for argv in (["env", "PATH=/nonexistent/bin", "tool"], ["sh", "-c", "PATH=/x tool"]):
        assert script in worktree_policy._argv_paths(argv, cwd=None, path_env=str(bindir))


def test_ps_line_with_a_wrapper_path_override_is_resolved(tmp_path, monkeypatch):
    import subprocess

    bindir, script = _tool_on_path(tmp_path)
    out = f" 78 {_UID} env PATH={bindir} tool serve PATH=/usr/bin HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )

    assert script in worktree_policy._ps_process_paths()


class _CountingPath(str):
    """A PATH string that counts how often it is split into directories."""

    splits = 0

    def split(self, *args, **kwargs):
        type(self).splits += 1
        return super().split(*args, **kwargs)


def test_which_filters_each_distinct_path_once_per_scan(tmp_path):
    """`_which` is called ~12k times a scan; it must not rebuild the dirs each call."""
    bindir, script = _tool_on_path(tmp_path)
    _CountingPath.splits = 0
    path_env = _CountingPath(f"relative:{bindir}:/nonexistent/bin")
    cache: dict = {}

    for name in ("tool", "missing", "tool", "other", "tool") * 20:
        worktree_policy._which(name, path_env, cache)

    assert worktree_policy._which("tool", path_env, cache) == script
    assert worktree_policy._which("missing", path_env, cache) is None
    assert _CountingPath.splits == 1
    # Only absolute entries count, as before.
    assert worktree_policy._which("tool", "relative:also/relative", cache) is None


def _ps_listing(lines: int, segments: int, path_dirs: int = 40) -> str:
    """A `ps -axwwE` listing: argv, then an env tail with a `;`-heavy LS_COLORS."""
    path = ":".join(f"/nonexistent/tool{i}/bin" for i in range(path_dirs))
    colors = "".join(f"*.e{i}=01;3{i % 8}:" for i in range(segments))
    env = [f"PATH={path}", f"LS_COLORS={colors}"] + [f"VAR{i}=value{i}" for i in range(22)]
    tail = " ".join(env)
    rows = (f"{pid} {_UID} /usr/bin/python3 -m worker --id {pid} {tail}\n" for pid in range(lines))
    return "".join(rows)


def test_ps_env_tail_is_not_reparsed_on_every_line(monkeypatch):
    """An env tail full of `;` split into pseudo-commands on every line (#2199).

    Four times the lines, each with a four times longer LS_COLORS, cost about
    sixteen times as much when every line re-parsed its own tail. A tail value
    is read once per scan, so the cost now grows with lines plus tail length.
    """
    import subprocess

    from growth_budget import assert_cpu_growth

    holder = {"out": ""}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=holder["out"], stderr=""),
    )

    def scan(listing: str) -> set[str]:
        holder["out"] = listing
        return worktree_policy._ps_process_paths()

    small, large = _ps_listing(40, 40), _ps_listing(160, 160)
    assert_cpu_growth(scan, small, large, limit=8, label="ps env tails")


def test_ps_env_tail_still_reads_a_program_after_an_operator(tmp_path, monkeypatch):
    """A `-c` string's last word can look like an assignment: `X=1;tool` runs tool."""
    import subprocess

    bindir, script = _tool_on_path(tmp_path)
    out = f" 79 {_UID} bash -c cd /srv; X=1;tool PATH={bindir} LS_COLORS=di=01;34:ln=01;36\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )

    assert script in worktree_policy._ps_process_paths()


# --- PATH override cost and relative entries (task #2209) --------------------


def _override_string(count: int) -> str:
    """A `-c` string of `count` distinct `PATH=` words and `count` bare words."""
    return " ".join(f"PATH=/nonexistent/v{i} w{i}" for i in range(count))


def _override_wrapper(count: int) -> list[str]:
    """`env PATH=/v0 ... PATH=/vN sh -c 'c0;c1;...'`: overrides carried into each command."""
    paths = [f"PATH=/nonexistent/v{i}" for i in range(count)]
    return ["env", *paths, "sh", "-c", ";".join(f"c{i}" for i in range(count))]


@pytest.mark.parametrize(
    "shape",
    ["string", "wrapper"],
)
def test_many_path_overrides_cost_grows_linearly(shape):
    """Every bare word was looked up on every distinct `PATH=` value (#2209).

    A 131 KB `-c` string of ~15k `PATH=/v_i` words and ~2k bare words made
    ~30M stats, minutes per scan; so did a wrapper carrying thousands of
    overrides into each of thousands of commands. Four times the words now
    cost about four times as much, not sixteen.
    """
    from growth_budget import assert_cpu_growth

    if shape == "string":
        small, large = _override_string(500), _override_string(2000)

        def scan(command):
            return worktree_policy._command_string_paths(command, None, "/nonexistent/bin", {}, 0)

    else:
        small, large = _override_wrapper(500), _override_wrapper(2000)

        def scan(argv):
            return worktree_policy._argv_paths(argv, cwd=None, path_env="/nonexistent/bin")

    assert_cpu_growth(scan, small, large, limit=8, label=f"PATH overrides ({shape})")


@pytest.mark.parametrize(
    "argv",
    [
        ["env", *[f"PATH=/nonexistent/v{i}" for i in range(200)], "PATH={bin}", "tool"],
        ["sh", "-c", " ".join(f"PATH=/nonexistent/v{i}" for i in range(200)) + " PATH={bin} tool"],
        [
            "bash",
            "-c",
            "; ".join(f"export PATH=/nonexistent/v{i}" for i in range(200))
            + "; export PATH={bin}:$PATH; tool",
        ],
        [
            "env",
            *[f"PATH=/nonexistent/v{i}" for i in range(200)],
            "sh",
            "-c",
            "PATH={bin} tool",
        ],
    ],
    ids=["wrapper", "assignments", "exports", "nested"],
)
def test_a_path_override_past_the_lookup_cap_still_marks_the_checkout(tmp_path, argv):
    """A capped lookup must not let a later `PATH=<wt>/bin` hide the checkout (#2209)."""
    bindir, _ = _tool_on_path(tmp_path)
    argv = [arg.replace("{bin}", str(bindir)) for arg in argv]

    paths = worktree_policy._argv_paths(argv, cwd=None, path_env="/nonexistent/bin")

    assert worktree_policy._in_use(tmp_path / "wt", frozenset(paths)) is True


@pytest.mark.parametrize(
    "argv,cwd",
    [
        (["env", "PATH=.venv/bin", "tool"], "wt"),
        (["env", "PATH=bin", "tool"], "wt/.venv"),
        (["sudo", "PATH=../wt/.venv/bin:$PATH", "tool"], "other"),
        (["sh", "-c", "PATH=../wt/.venv/bin tool"], "other"),
        (["sh", "-c", "export PATH=.venv/bin; tool"], "wt"),
        (["env", "PATH=bin", "sh", "-c", "tool"], "wt/.venv"),
        # An empty entry is the cwd, as in a shell.
        (["env", "PATH=:/nonexistent/bin", "tool"], "wt/.venv/bin"),
    ],
)
def test_a_relative_path_override_resolves_against_the_process_cwd(tmp_path, argv, cwd):
    """`env PATH=bin tool` runs <cwd>/bin/tool; relative entries were dropped (#2209)."""
    bindir, script = _tool_on_path(tmp_path)
    (tmp_path / "other").mkdir(exist_ok=True)

    paths = worktree_policy._argv_paths(argv, cwd=str(tmp_path / cwd), path_env="/nonexistent/bin")

    assert script in paths


def test_a_relative_path_override_without_a_cwd_is_skipped(tmp_path, monkeypatch):
    """With no cwd to anchor it, a relative entry names nothing; the hub's cwd is not used."""
    bindir, script = _tool_on_path(tmp_path)
    monkeypatch.chdir(bindir.parent)

    paths = worktree_policy._argv_paths(
        ["env", "PATH=bin", "tool"], cwd=None, path_env="/nonexistent/bin"
    )

    assert script not in paths


# --- A process's own long PATH (task #2209 follow-up) ------------------------


def _long_path(dirs: int, *extra: str) -> str:
    return ":".join([*(f"/nonexistent/p{i}" for i in range(dirs)), *extra])


def test_a_long_process_path_costs_linearly():
    """A process PATH of ~15k dirs times ~2k bare words was ~30M stats (#2209).

    The process's own PATH was searched whole for every bare word. It is now
    searched up to the same 64-directory cap as an override, so four times
    the words and four times the directories cost about four times as much.
    """
    from growth_budget import assert_cpu_growth

    def words(count):
        return " ".join(f"w{i}" for i in range(count))

    small = (words(500), _long_path(3750))
    large = (words(2000), _long_path(15000))

    class _Pair(tuple):
        def __len__(self):  # assert_cpu_growth reports the size ratio
            return len(self[0]) + len(self[1])

    def scan(pair):
        command, path_env = pair
        return worktree_policy._command_string_paths(command, None, path_env, {}, 0)

    assert_cpu_growth(scan, _Pair(small), _Pair(large), limit=8, label="long process PATH")


def test_a_worktree_dir_past_the_cap_in_a_proc_path_keeps_the_checkout(tmp_path):
    """The directories a capped lookup skips are kept as live paths (#2209)."""
    bindir, _ = _tool_on_path(tmp_path)
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cmdline").write_bytes(b"tool\0serve\0")
    (proc / "10" / "environ").write_bytes(f"PATH={_long_path(200, str(bindir))}\0".encode())

    paths = worktree_policy._proc_process_paths(proc)

    assert paths is not None
    assert worktree_policy._in_use(tmp_path / "wt", paths) is True


def test_a_worktree_dir_past_the_cap_in_a_ps_path_keeps_the_checkout(tmp_path, monkeypatch):
    import subprocess

    bindir, _ = _tool_on_path(tmp_path)
    out = f" 81 {_UID} tool serve PATH={_long_path(200, str(bindir))} HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )

    paths = frozenset(worktree_policy._ps_process_paths())

    assert worktree_policy._in_use(tmp_path / "wt", paths) is True


def test_a_short_process_path_adds_no_directories_as_paths(tmp_path):
    """Under the cap nothing changes: PATH dirs are searched, not kept as paths."""
    bindir, script = _tool_on_path(tmp_path)
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cmdline").write_bytes(b"tool\0")
    (proc / "10" / "environ").write_bytes(f"PATH=/usr/bin:{bindir}\0".encode())

    paths = worktree_policy._proc_process_paths(proc)

    assert script in paths
    assert str(bindir) not in paths and "/usr/bin" not in paths


# --- Coverage past the PATH cap and in the env tail (task #2226) -------------


@pytest.mark.parametrize("shape", ["wrapper", "string", "own-path"])
def test_a_linked_program_past_the_path_cap_keeps_the_checkout(tmp_path, shape):
    """`<other>/bin/tool -> <wt>/.venv/bin/tool` in a directory past the cap.

    Only the directory was kept, and the link's target lies under neither it
    nor its realpath; a lookup within the cap follows the link (#2226).
    """
    _, script = _tool_on_path(tmp_path)
    other = tmp_path / "other" / "bin"
    other.mkdir(parents=True)
    (other / "tool").symlink_to(script)
    many = [f"PATH=/nonexistent/v{i}" for i in range(200)]
    if shape == "wrapper":
        paths = worktree_policy._argv_paths(
            ["env", *many, f"PATH={other}", "tool"], cwd=None, path_env="/nonexistent/bin"
        )
    elif shape == "string":
        command = " ".join(many) + f" PATH={other} tool"
        paths = worktree_policy._argv_paths(
            ["sh", "-c", command], cwd=None, path_env="/nonexistent/bin"
        )
    else:
        path_env = _long_path(200, str(other))
        paths = worktree_policy._argv_paths(["tool"], cwd=None, path_env=path_env)
        paths |= set(worktree_policy._path_overflow(path_env, set()))

    assert worktree_policy._in_use(tmp_path / "wt", frozenset(paths)) is True


def test_an_env_tail_path_assignment_resolves_against_the_cwd(tmp_path):
    """`sh -c 'export PATH=bin;tool'` leaves `PATH=bin;tool` in the `-E` tail,
    read alone and without the cwd unless it held a `/` (#2226)."""
    bindir, script = _tool_on_path(tmp_path)
    tokens = ["sh", "-c", "export", "PATH=bin;tool", "HOME=/h"]
    paths: set[str] = set()

    worktree_policy._ps_line_paths(tokens, str(bindir.parent), paths, {}, {}, set())

    assert script in paths


# --- Symlink then `..` in a PATH entry (review of #2209 at 01062250) ---------


def _symlinked_tool(root: Path, wt: Path, *, link_outside: bool) -> str:
    """wt/actual/bin/reviewtool, and a `link -> wt/actual/nested` symlink.

    The kernel resolves `<link>/../bin` by traversing `link` first, so it is
    wt/actual/bin. Collapsing `..` lexically instead gives `<dir of link>/bin`,
    which does not exist. With the link outside the worktree, that lexical
    directory lies outside it too.
    """
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "bin").mkdir()
    tool = wt / "actual" / "bin" / "reviewtool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    link = (root / "outside" if link_outside else wt) / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(wt / "actual" / "nested")
    return f"{link}/../bin"


def _loop(path_value: str) -> list[str]:
    return [
        "/bin/sh",
        "-c",
        f"export PATH={path_value}; while :; do reviewtool; /bin/sleep 30; done",
    ]


@pytest.mark.parametrize("link_outside", [False, True], ids=["link-in-wt", "link-outside"])
@pytest.mark.parametrize(
    "shape",
    ["export", "relative-export", "wrapper", "past-the-cap", "own-path-past-the-cap"],
)
def test_a_symlink_then_dotdot_path_entry_keeps_the_checkout(tmp_path, link_outside, shape):
    """`<link>/../bin` is searched as the kernel resolves it, not collapsed (P1)."""
    wt = tmp_path / "wt"
    other = tmp_path / "other"
    other.mkdir()
    entry = _symlinked_tool(tmp_path, wt, link_outside=link_outside)
    path_env = "/usr/bin"
    cwd = str(other)
    many = [f"/nonexistent/v{i}" for i in range(200)]
    if shape == "export":
        argv = _loop(entry)
    elif shape == "relative-export":
        # relpath of the link itself: relpath would collapse `link/..` too.
        link = entry.removesuffix("/../bin")
        argv = _loop(os.path.relpath(link, cwd) + "/../bin")
    elif shape == "wrapper":
        argv = ["env", f"PATH={entry}", "reviewtool"]
    elif shape == "past-the-cap":
        argv = ["env", *(f"PATH={d}" for d in many), f"PATH={entry}", "reviewtool"]
    else:
        argv = ["reviewtool"]
        path_env = ":".join([*many, entry])

    paths = set(worktree_policy._argv_paths(argv, cwd=cwd, path_env=path_env))
    paths.update(worktree_policy._path_overflow(path_env, set()))

    assert worktree_policy._in_use(wt, frozenset(paths)) is True


@pytest.mark.parametrize("link_outside", [False, True], ids=["link-in-wt", "link-outside"])
def test_a_symlink_then_dotdot_loop_keeps_the_checkout_in_both_scans(
    tmp_path, monkeypatch, link_outside
):
    """The reviewer's shell loop, through /proc, `ps` and the cleanup decision."""
    import subprocess

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-live")
    entry = _symlinked_tool(tmp_path, wt, link_outside=link_outside)
    argv = _loop(entry)

    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "10" / "environ").write_bytes(b"PATH=/usr/bin\0")
    proc_paths = worktree_policy._proc_process_paths(proc)
    assert proc_paths is not None
    assert worktree_policy._in_use(wt, proc_paths) is True

    out = f" 10 {_UID} {' '.join(argv)} PATH=/usr/bin HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert worktree_policy._in_use(wt, frozenset(worktree_policy._ps_process_paths())) is True

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: proc_paths)
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff) == (None, False, "in_use")
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


# --- Symlink then `..` in argv and code-path env (review follow-up) ----------


def _linked_checkout(tmp_path: Path, wt: Path) -> Path:
    """wt/actual/{run.py,src,.venv}, and other/link -> wt/actual/nested.

    From cwd `other`, `link/../run.py` is wt/actual/run.py to the kernel,
    but `other/run.py` once `..` is collapsed lexically.
    """
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "src").mkdir()
    (wt / "actual" / ".venv").mkdir()
    (wt / "actual" / "run.py").write_text("")
    other = tmp_path / "other"
    other.mkdir()
    (other / "link").symlink_to(wt / "actual" / "nested")
    return other


def _argv_and_env(other: Path, shape: str) -> tuple[list[str], list[str]]:
    link = f"{other}/link"
    if shape == "relative-arg":
        return ["/usr/bin/python3", "link/../run.py"], ["PATH=/usr/bin"]
    if shape == "absolute-arg":
        return ["/usr/bin/python3", f"{link}/../run.py"], ["PATH=/usr/bin"]
    if shape == "shell-arg":
        return ["/bin/sh", "-c", "exec python3 link/../run.py --serve"], ["PATH=/usr/bin"]
    if shape == "pythonpath":
        return ["/usr/bin/python3", "-m", "pkg"], ["PATH=/usr/bin", f"PYTHONPATH={link}/../src"]
    return ["/usr/bin/python3", "-m", "pkg"], ["PATH=/usr/bin", f"VIRTUAL_ENV={link}/../.venv"]


_ARG_SHAPES = ["relative-arg", "absolute-arg", "shell-arg", "pythonpath", "virtualenv"]


@pytest.mark.parametrize("shape", _ARG_SHAPES)
def test_a_symlink_then_dotdot_argv_or_env_path_keeps_the_checkout(tmp_path, monkeypatch, shape):
    """Process-supplied paths are kept raw, normalized and resolved (review P1 class).

    `os.path.normpath(cwd + "/link/../run.py")` named other/run.py while the
    process ran wt/actual/run.py, so the checkout read as idle.
    """
    import subprocess

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-argv")
    other = _linked_checkout(tmp_path, wt)
    argv, env = _argv_and_env(other, shape)

    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cwd").unlink()
    (proc / "10" / "cwd").symlink_to(other)
    (proc / "10" / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "10" / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
    proc_paths = worktree_policy._proc_process_paths(proc)
    assert proc_paths is not None
    assert worktree_policy._in_use(wt, proc_paths) is True

    out = f" 10 {_UID} {' '.join(argv)} {' '.join(env)} HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    ps_paths = frozenset(worktree_policy._ps_process_paths({"10": str(other)}))
    assert worktree_policy._in_use(wt, ps_paths) is True

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: proc_paths)
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff) == (None, False, "in_use")
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def _scan_path_tokens(text):
    # Module-level and fixture-free, so the growth cache guard can follow it.
    # Relative tokens resolve against "/", where none of them exist, the same
    # as under an empty tmp_path.
    return worktree_policy._command_string_paths(text, "/", "/nonexistent", {}, 0)


def test_path_tokens_are_resolved_in_linear_time():
    """Resolving each path token costs per token, not per string squared."""
    from growth_budget import assert_cpu_growth

    def command(count):
        return " ".join(f"link{i}/../run{i}.py /n/{'d/' * 20}x{i}" for i in range(count))

    assert_cpu_growth(_scan_path_tokens, command(250), command(1000), limit=8, label="path tokens")


def test_an_over_long_path_token_is_kept_unresolved():
    """A path longer than PATH_MAX cannot be opened, so it is not resolved (bounded cost)."""
    token = "/n" + "/a" * 40000
    paths = worktree_policy._argv_paths(["/bin/true", token], cwd=None, path_env="/nonexistent")
    assert token in paths


# --- Relative PYTHONPATH / VIRTUAL_ENV entries (review follow-up) -------------


_RELATIVE_ENV = {
    "pythonpath": ["PYTHONPATH=../review-env/src"],
    "pythonpath-later-entry": ["PYTHONPATH=/nonexistent/lib:../review-env/src"],
    "pythonpath-symlink-dotdot": ["PYTHONPATH=link/../src"],
    "virtualenv": ["VIRTUAL_ENV=../review-env/.venv"],
}


@pytest.mark.parametrize("shape", list(_RELATIVE_ENV))
def test_a_relative_code_path_env_entry_keeps_the_checkout(tmp_path, monkeypatch, shape):
    """Python reads a relative PYTHONPATH entry against its cwd; it was ignored."""
    import subprocess

    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-env")
    (wt / "src").mkdir()
    (wt / ".venv").mkdir()
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "src").mkdir()
    other = ws / ".worktrees" / "other-cwd"
    other.mkdir()
    (other / "link").symlink_to(wt / "actual" / "nested")
    argv = ["/usr/bin/python3", "-m", "pkg"]
    env = ["PATH=/usr/bin", *_RELATIVE_ENV[shape]]

    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cwd").unlink()
    (proc / "10" / "cwd").symlink_to(other)
    (proc / "10" / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "10" / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
    proc_paths = worktree_policy._proc_process_paths(proc)
    assert proc_paths is not None
    assert worktree_policy._in_use(wt, proc_paths) is True

    out = f" 10 {_UID} {' '.join(argv)} {' '.join(env)} HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    ps_paths = frozenset(worktree_policy._ps_process_paths({"10": str(other)}))
    assert worktree_policy._in_use(wt, ps_paths) is True

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: proc_paths)
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff) == (None, False, "in_use")
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def test_a_relative_code_path_env_entry_without_a_cwd_is_skipped(tmp_path, monkeypatch):
    """No process cwd: the entry names nothing. The hub's own cwd is never used."""
    import subprocess

    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # `wt/src` from the hub's cwd would hit the checkout
    out = f" 10 {_UID} /usr/bin/python3 -m pkg PATH=/usr/bin PYTHONPATH=wt/src VIRTUAL_ENV=wt HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )

    assert worktree_policy._in_use(wt, frozenset(worktree_policy._ps_process_paths())) is False
    assert worktree_policy._env_code_paths(["PYTHONPATH=wt/src"]) == set()


# --- Bounded, fail-closed resolution (review of #2209 at 7f09f0f5) ------------


def _proc_running(tmp_path: Path, argv: list[str], cwd: Path, env: list[str]) -> Path:
    proc = _fake_proc(tmp_path, own_status=_OWN, procs={"10": _OWN})
    (proc / "10" / "cwd").unlink()
    (proc / "10" / "cwd").symlink_to(cwd)
    (proc / "10" / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "10" / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
    return proc


def _ps_line(monkeypatch, argv: list[str], env: list[str]) -> None:
    import subprocess

    out = f" 10 {_UID} {' '.join(argv)} {' '.join(env)} HOME=/h\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )


@pytest.mark.parametrize("shape", ["argv", "pythonpath"])
def test_a_long_relative_path_the_kernel_resolves_keeps_the_checkout(tmp_path, monkeypatch, shape):
    """P1: a relative path under PATH_MAX can exceed 4096 chars once joined to the cwd.

    The kernel resolves it from the cwd; skipping resolution past 4096 joined
    chars dropped the only form inside the checkout.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-live")
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "src").mkdir()
    (wt / "actual" / "run.sh").write_text("echo ready\n")
    other = tmp_path / ("c" * 120) / "other-cwd"
    other.mkdir(parents=True)
    (other / "link").symlink_to(wt / "actual" / "nested")
    rel = "./" * 2000 + ("link/../run.sh" if shape == "argv" else "link/../src")
    assert len(rel) < 4096 < len(os.path.join(str(other), rel))
    if shape == "argv":
        argv, env = ["/bin/sh", rel], ["PATH=/usr/bin"]
    else:
        argv, env = ["/usr/bin/python3", "-m", "pkg"], ["PATH=/usr/bin", f"PYTHONPATH={rel}"]

    proc_paths = worktree_policy._proc_process_paths(_proc_running(tmp_path, argv, other, env))
    assert proc_paths is not None
    assert worktree_policy._in_use(wt, proc_paths) is True

    _ps_line(monkeypatch, argv, env)
    ps_paths = frozenset(worktree_policy._ps_process_paths({"10": str(other)}))
    assert worktree_policy._in_use(wt, ps_paths) is True

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: proc_paths)
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert worktree_policy._worktree_staleness(wt, {}, cutoff) == (None, False, "in_use")
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def _link_chain(base: Path, count: int, *, detour: int = 0) -> Path:
    """l0 -> l1 -> ... -> l<count-1> -> file, each target optionally padded with
    `d/../` repeats, the reviewer's expansion shape."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "d").mkdir(exist_ok=True)
    (base / "file").write_text("")
    for i in range(count):
        target = "file" if i == count - 1 else f"l{i + 1}"
        (base / f"l{i}").symlink_to("d/../" * detour + target)
    return base / "l0"


def _link_loop(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "a").symlink_to("b")
    (base / "b").symlink_to("a")
    return base / "a" / "run.py"


@pytest.mark.parametrize("kind", ["loop", "deep-chain", "expanding-chain"])
def test_an_unresolvable_path_leaves_liveness_unknown(tmp_path, monkeypatch, kind):
    """P2: a path the kernel would refuse (ELOOP) is not silently kept or dropped.

    The scan reports liveness unknown, naming the pid, so cleanup removes
    nothing on its basis.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    _stub_worktree(ws, "review-unknown")
    base = tmp_path / "links"
    if kind == "loop":
        token = _link_loop(base)
    elif kind == "deep-chain":
        token = _link_chain(base, 100)
    else:
        token = _link_chain(base, 60, detour=200)
    argv, env = ["/usr/bin/python3", str(token)], ["PATH=/usr/bin"]

    paths, cause = worktree_policy._scan_proc(_proc_running(tmp_path, argv, tmp_path, env))
    assert paths is None
    assert "pid 10" in cause and str(token) not in cause

    _ps_line(monkeypatch, argv, env)
    with pytest.raises(worktree_policy._Unresolvable):
        worktree_policy._ps_process_paths()

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: paths)
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


@pytest.mark.parametrize("kind", ["deep-chain", "expanding-chain"])
def test_resolution_is_iterative_and_bounded_like_the_kernel(tmp_path, monkeypatch, kind):
    """No recursion (realpath recursed per link on Python 3.11) and bounded syscalls.

    At most 40 links are followed (MAXSYMLINKS); the reviewer's 1,200-link
    expanding chain made realpath do ~900k lstat calls.
    """
    import inspect
    import sys

    base = tmp_path / "links"
    token = _link_chain(base, 100) if kind == "deep-chain" else _link_chain(base, 60, detour=200)
    calls = {"n": 0}
    real_lstat = os.lstat

    def counting(*a, **k):
        calls["n"] += 1
        return real_lstat(*a, **k)

    # Measure the stack before counting starts: inspect.stack() reads source
    # files through linecache, whose lstat calls depend on what is imported,
    # not on the resolver (review r2).
    depth = len(inspect.stack())
    monkeypatch.setattr(worktree_policy.os, "lstat", counting)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + 30)
    try:
        with pytest.raises(worktree_policy._Unresolvable, match="symbolic links"):
            worktree_policy._Resolver().resolve(str(token))
    finally:
        sys.setrecursionlimit(limit)
    # 41 link targets of at most 401 components each, plus the path itself.
    assert calls["n"] <= 41 * 402 + 20


def test_a_spent_resolution_budget_is_unknown_not_dropped(tmp_path):
    deep = tmp_path / "/".join(f"d{i}" for i in range(50))
    deep.mkdir(parents=True)
    with pytest.raises(worktree_policy._Unresolvable, match="budget"):
        worktree_policy._Resolver(budget=20).resolve(str(deep / "file"))


@pytest.mark.live_process_scan
def test_an_unresolvable_path_in_the_lsof_scan_is_unknown(monkeypatch):
    """`_live_process_paths` maps an unresolvable path to unknown, with its reason."""

    def scan(proc):
        raise worktree_policy._Unresolvable("too many symbolic links", "pid 7 (sh)")

    monkeypatch.setattr(worktree_policy, "_scan_proc", scan)
    assert worktree_policy._live_process_paths() is None
    assert "pid 7 (sh)" in worktree_policy._last_unknown_cause


# --- Round 3: errors that do not prove a path unused (review at 4b4130da) -----


def _long_cwd(tmp_path: Path, total: int) -> Path:
    """A real directory whose absolute name is `total` bytes long."""
    cwd = tmp_path / "o"
    while len(str(cwd)) < total:
        room = total - len(str(cwd)) - 1
        cwd = cwd / ("p" * min(200, max(room, 1)))
    cwd.mkdir(parents=True)
    assert len(str(cwd)) == total
    return cwd


def _assert_unknown_everywhere(tmp_path, monkeypatch, argv, cwd, env=("PATH=/usr/bin",)):
    """The /proc scan is unknown naming the pid, `ps` raises, cleanup removes nothing."""
    env = list(env)
    paths, cause = worktree_policy._scan_proc(_proc_running(tmp_path, argv, cwd, env))
    assert paths is None, "liveness must be unknown"
    assert "pid 10" in cause
    _ps_line(monkeypatch, argv, env)
    with pytest.raises(worktree_policy._Unresolvable):
        worktree_policy._ps_process_paths({"10": str(cwd)})
    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: paths)
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def test_enametoolong_on_the_joined_path_is_unknown(tmp_path, monkeypatch):
    """P1: the reviewer's 255-byte relative path from a 3,866-byte cwd (4,122 joined).

    The process opens it relative to its cwd; the scanner's absolute lstat
    fails with ENAMETOOLONG, which does not prove the path unused.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-live")
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "run.sh").write_text("echo ready\n")
    cwd = _long_cwd(tmp_path, 3866)
    sub = "s" * 240
    # Past PATH_MAX once joined, so made relative to directory descriptors.
    cwd_fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.mkdir(sub, dir_fd=cwd_fd)
        sub_fd = os.open(sub, os.O_RDONLY | os.O_DIRECTORY, dir_fd=cwd_fd)
        try:
            os.symlink(wt / "actual" / "nested", "link", dir_fd=sub_fd)
            # The process's own relative stat succeeds: the path is valid.
            os.stat("link/../run.sh", dir_fd=sub_fd)
        finally:
            os.close(sub_fd)
    finally:
        os.close(cwd_fd)
    rel = f"{sub}/link/../run.sh"
    assert len(rel) == 255 and len(os.path.join(str(cwd), rel)) > 4096

    _assert_unknown_everywhere(tmp_path, monkeypatch, ["/bin/sh", rel], cwd)


def test_a_component_longer_than_name_max_names_nothing(tmp_path):
    """No process can open a path with a >255-byte component, so it is not unknown."""
    token = "x" * 300 + "/run.py"  # a base64-ish argv word holding a `/`
    paths, cause = worktree_policy._scan_proc(
        _proc_running(tmp_path, ["/usr/bin/python3", token], tmp_path, ["PATH=/usr/bin"])
    )
    assert cause is None and paths is not None


@pytest.fixture
def _gated(tmp_path):
    """other/gate/link -> wt/actual/nested, with `gate` made unsearchable (mode 000)."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-gated")
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "run.sh").write_text("echo ready\n")
    other = tmp_path / "other"
    (other / "gate").mkdir(parents=True)
    (other / "gate" / "link").symlink_to(wt / "actual" / "nested")
    (other / "gate").chmod(0)
    try:
        yield wt, other
    finally:
        (other / "gate").chmod(0o755)


def test_a_denied_component_for_a_same_credential_process_is_unknown(tmp_path, monkeypatch, _gated):
    """P1: a same-credential script still runs after its directory became unsearchable."""
    _, other = _gated
    _assert_unknown_everywhere(tmp_path, monkeypatch, ["/bin/sh", "gate/link/../run.sh"], other)


def test_a_denied_component_for_another_users_process_ends_the_walk(tmp_path, monkeypatch, _gated):
    """Another user's process keeps the existing boundary: its paths are not unknown."""

    _, other = _gated
    argv = ["/bin/sh", "gate/link/../run.sh"]
    proc = _proc_running(tmp_path, argv, other, ["PATH=/usr/bin"])
    (proc / "10" / "status").write_text(_OTHER)
    paths, cause = worktree_policy._scan_proc(proc)
    assert cause is None and paths is not None
    # `ps` reports another uid for it (round 4: ownership is read, not inferred).
    _ps_and_lsof(monkeypatch, [("10", _other_uid(), " ".join(argv))], other)
    assert worktree_policy._ps_process_paths({"10": str(other)}) is not None


# --- Round 4 (review at e66d4cf6) ---------------------------------------------


def test_a_multibyte_component_valid_on_apfs_is_looked_up(tmp_path, monkeypatch):
    """P1: APFS allows 255 characters, so 128 x "é" (256 UTF-8 bytes) is a valid name.

    It is looked up, never assumed missing. Linux cannot create that name, so
    the filesystem edge is modelled the reviewer's way: the long component is
    translated to a real short symlink for lstat/readlink.
    """
    ws = config.workspace_root()
    ws.mkdir(parents=True, exist_ok=True)
    wt = _stub_worktree(ws, "review-apfs")
    (wt / "actual" / "nested").mkdir(parents=True)
    (wt / "actual" / "run.sh").write_text("echo ready\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / "short").symlink_to(wt / "actual" / "nested")
    name = "é" * 128
    assert len(name) <= 255 < len(name.encode())
    real_lstat, real_readlink = os.lstat, os.readlink

    def apfs(path):
        return str(path).replace(name, "short")

    monkeypatch.setattr(worktree_policy.os, "lstat", lambda p, *a, **k: real_lstat(apfs(p)))
    monkeypatch.setattr(worktree_policy.os, "readlink", lambda p, *a, **k: real_readlink(apfs(p)))

    paths, cause = worktree_policy._scan_proc(
        _proc_running(tmp_path, ["/bin/sh", f"{name}/../run.sh"], other, ["PATH=/usr/bin"])
    )
    assert cause is None
    assert worktree_policy._in_use(wt, paths) is True


def test_a_component_past_any_filesystem_name_limit_names_nothing(tmp_path):
    """Over 1,024 UTF-8 bytes no filesystem the hub runs on (255 bytes, 255 UTF-16
    units, 255 characters) accepts the name, so it is not unknown."""
    token = "é" * 600 + "/run.py"
    paths, cause = worktree_policy._scan_proc(
        _proc_running(tmp_path, ["/usr/bin/python3", token], tmp_path, ["PATH=/usr/bin"])
    )
    assert cause is None and paths is not None


def _ps_and_lsof(monkeypatch, rows, cwd: Path, separate_uids: str | None = None) -> None:
    """Stub `ps` and lsof. `rows` are (pid, uid, args), uid "-" when `ps` shows none.

    A one-query read (`pid=,uid=,args=`) gets all three columns. A separate
    `pid=,uid=` read gets `separate_uids`, a second snapshot taken after the
    first (a pid reused meanwhile reads another uid); by default the rows'.
    lsof gives every pid the cwd `cwd`.
    """
    import subprocess

    def run(argv, *a, **k):
        spec = argv[-1] if argv[0] == "ps" else ""
        if argv[0] == "lsof":
            out = "".join(f"p{pid}\nfcwd\nn{cwd}\n" for pid, _, _ in rows)
        elif spec == "pid=,uid=":
            default = "".join(f" {pid} {uid}\n" for pid, uid, _ in rows)
            out = default if separate_uids is None else separate_uids
        elif spec == "pid=,args=":
            out = "".join(f" {pid} {args}\n" for pid, _, args in rows)
        else:
            out = "".join(f" {pid} {uid} {args}\n" for pid, uid, args in rows)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", run)


def _other_uid() -> int:
    return 0 if os.geteuid() != 0 else 1


@pytest.mark.parametrize(
    "tail",
    ["", " PATH=/usr/bin REVIEW_LABEL=two words"],
    ids=["no-environment", "space-ended-environment"],
)
def test_a_same_user_ps_line_without_a_clean_env_tail_is_strict(
    tmp_path, monkeypatch, _gated, tail
):
    """P1: ownership comes from the uid `ps` reports, not from the environment's shape."""
    _, other = _gated
    _ps_and_lsof(monkeypatch, [("10", os.geteuid(), f"/bin/sh gate/link/../run.sh{tail}")], other)

    with pytest.raises(worktree_policy._Unresolvable, match="pid 10"):
        worktree_policy._lsof_process_paths()

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: None)
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def test_a_ps_line_whose_uid_is_unknown_is_strict(tmp_path, monkeypatch, _gated):
    """No uid for the pid (`ps` shows none, or a separate read misses it): fail closed."""
    _, other = _gated
    _ps_and_lsof(monkeypatch, [("10", "-", "/bin/sh gate/link/../run.sh")], other, " 99 0\n")
    with pytest.raises(worktree_policy._Unresolvable):
        worktree_policy._ps_process_paths({"10": str(other)})


def test_another_users_ps_line_keeps_the_denied_boundary(tmp_path, monkeypatch, _gated):
    _, other = _gated
    rows = [("10", _other_uid(), "/bin/sh gate/link/../run.sh PATH=/usr/bin")]
    _ps_and_lsof(monkeypatch, rows, other)
    assert worktree_policy._ps_process_paths({"10": str(other)}) is not None


# --- Round 5 (review at f557f2ad) ---------------------------------------------


@pytest.mark.parametrize("second_uid", ["own", "-"], ids=["same-user", "no-uid"])
def test_a_foreign_rows_cached_tail_never_relaxes_a_strict_row(
    tmp_path, monkeypatch, _gated, second_uid
):
    """P1: two `ps` rows share cwd, PATH and the tail `X=1;gate/link/../run.sh`.

    Another user's row comes first and reads the tail permissively. The second
    row, ours or of unknown owner, must still resolve it strictly, not reuse
    the permissive result: the denied component is unknown for it.
    """
    _, other = _gated
    tail = "/bin/sh X=1;gate/link/../run.sh"
    uid = os.geteuid() if second_uid == "own" else "-"
    rows = [("9", _other_uid(), tail), ("10", uid, tail)]
    _ps_and_lsof(monkeypatch, rows, other)
    with pytest.raises(worktree_policy._Unresolvable, match="pid 10"):
        worktree_policy._lsof_process_paths()

    removed = _removal_recorder(monkeypatch)
    monkeypatch.setattr(worktree_policy, "_live_process_paths", lambda: None)
    cleanup_stale_worktrees(dry_run=False, max_idle_days=0.0)
    assert removed == []


def test_a_pid_reused_between_ps_reads_cannot_relax_its_row(tmp_path, monkeypatch, _gated):
    """P1: the uid comes from the same `ps` row as the command line.

    A separate uid read taken later can find the pid reused by another user's
    process; that uid must not make the first process's row permissive.
    """
    _, other = _gated
    rows = [("10", os.geteuid(), "/bin/sh gate/link/../run.sh PATH=/usr/bin")]
    _ps_and_lsof(monkeypatch, rows, other, separate_uids=f" 10 {_other_uid()}\n")
    with pytest.raises(worktree_policy._Unresolvable, match="pid 10"):
        worktree_policy._ps_process_paths({"10": str(other)})


@pytest.mark.parametrize("link_home", ["HOME", "UA_HOME"])
@pytest.mark.parametrize("link_name", ["current", "previous"])
def test_release_link_target_is_never_stale(tmp_path, monkeypatch, link_home, link_name):
    """A node release target is kept whatever its name (task #2298).

    Deleting the live release worktree put a node into a 203/EXEC restart loop
    (task #2292). The name guard covers `release-*`; this covers a target named
    anything else, under either the user home or UA_HOME.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    base = home / ".unified_agent" if link_home == "HOME" else config.data_home()
    wt = tmp_path / "repo" / ".worktrees" / "node-build"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/node-build\n")
    old = time.time() - 30 * 86400
    os.utime(wt, (old, old))
    cutoff = datetime.now(UTC) - timedelta(days=7)

    assert worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset()) == (
        None,
        True,
        "idle_timeout",
    )

    (base / "release").mkdir(parents=True)
    link = base / "release" / link_name
    link.symlink_to(wt)

    assert worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset()) == (
        None,
        False,
        "deploy_checkout",
    )

    # A relative target resolves against the link's directory, not the cwd.
    link.unlink()
    link.symlink_to(os.path.relpath(wt, link.parent))
    monkeypatch.chdir(tmp_path / "repo")
    assert worktree_policy._worktree_staleness(wt, {}, cutoff, frozenset()) == (
        None,
        False,
        "deploy_checkout",
    )
