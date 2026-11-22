"""doctor's package_checkout row: the shared venv must not run a worktree's code (task #1944).

The desktop's ``~/dev/unified_agent/.venv`` editable pointer was left aimed at
``.worktrees/task-1927-batch/src``, an earlier session's per-task worktree. Every
service importing through that venv then ran that stale tree whatever main
said. The row is DEGRADED when the running package, or the main checkout's
shared venv, resolves into a per-task worktree.
"""

from __future__ import annotations

from pathlib import Path

from unified_agent.tools import doctor

PTH = "_editable_impl_unified_agent.pth"


def _pth(venv: Path, target: Path) -> Path:
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    path = site / PTH
    path.write_text(f"{target}\n", encoding="utf-8")
    return path


def test_the_shared_venv_importing_a_task_worktree_is_degraded(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "task-1927-batch"
    worktree.mkdir(parents=True)
    row = doctor._package_checkout_check(checkout=worktree, prefix=repo / ".venv")
    assert row["capability"] == "package_checkout"
    assert row["status"] == "DEGRADED"
    assert str(worktree) in row["detail"]
    assert str(repo / "src") in row["detail"]  # where it should point instead


def test_a_shared_venv_pth_aimed_at_a_worktree_is_degraded_from_the_main_checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    stale = repo / ".worktrees" / "task-1927-batch" / "src"
    _pth(repo / ".venv", stale)
    row = doctor._package_checkout_check(checkout=repo, prefix=tmp_path / "elsewhere-venv")
    assert row["status"] == "DEGRADED"
    assert PTH in row["detail"] and str(stale) in row["detail"]


def test_a_claude_agent_worktree_counts_as_a_task_worktree(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".claude" / "worktrees" / "agent-abc"
    worktree.mkdir(parents=True)
    row = doctor._package_checkout_check(checkout=worktree, prefix=repo / ".venv")
    assert row["status"] == "DEGRADED"
    assert str(repo / "src") in row["detail"]


def test_a_worktree_running_its_own_venv_is_fine(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "task-7"
    _pth(worktree / ".venv", worktree / "src")
    _pth(repo / ".venv", repo / "src")
    row = doctor._package_checkout_check(checkout=worktree, prefix=worktree / ".venv")
    assert row["status"] == "LIVE", row


def test_the_main_checkout_with_a_healthy_pointer_is_live(tmp_path):
    repo = tmp_path / "repo"
    _pth(repo / ".venv", repo / "src")
    row = doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")
    assert row["status"] == "LIVE"
    assert str(repo) in row["detail"]


def test_system_doctor_carries_the_row():
    rows = [r for r in doctor.system_doctor()["checks"] if r["capability"] == "package_checkout"]
    assert len(rows) == 1
    assert set(rows[0]) == {"capability", "status", "detail", "gate"}


# --- review of PR #1116: any pointer off the main checkout's src -------------


def test_a_shared_venv_pth_aimed_at_any_other_tree_is_degraded(tmp_path):
    repo = tmp_path / "unified_agent"
    (repo / "src").mkdir(parents=True)
    old = tmp_path / "unified_agent-old" / "src"
    old.mkdir(parents=True)
    _pth(repo / ".venv", old)
    row = doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")
    assert row["status"] == "DEGRADED"
    assert str(old) in row["detail"]


def test_a_pointer_reaching_a_worktree_through_a_symlink_is_degraded(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    worktree = repo / ".worktrees" / "task-9"
    (worktree / "src").mkdir(parents=True)
    link = tmp_path / "looks-fine"
    link.symlink_to(worktree)
    _pth(repo / ".venv", link / "src")
    row = doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")
    assert row["status"] == "DEGRADED"


def test_a_relative_pth_entry_resolves_from_its_site_packages(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    pth = _pth(repo / ".venv", repo / "src")
    pth.write_text("../../../../src\n", encoding="utf-8")
    assert doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")["status"] == "LIVE"
    (repo / ".worktrees" / "task-3" / "src").mkdir(parents=True)
    pth.write_text("../../../../.worktrees/task-3/src\n", encoding="utf-8")
    assert doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")["status"] == (
        "DEGRADED"
    )


def test_a_leading_worktrees_component_is_a_task_worktree():
    split = doctor._split_task_worktree(Path(".worktrees/task-1/src"))
    assert split == (Path("."), Path(".worktrees/task-1"))


def _finder(venv: Path, target: Path) -> None:
    """A setuptools-style editable install: an import-line .pth plus a finder."""
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "__editable__.unified_agent-0.3.2.pth").write_text(
        "import __editable___unified_agent_0_3_2_finder; "
        "__editable___unified_agent_0_3_2_finder.install()\n",
        encoding="utf-8",
    )
    (site / "__editable___unified_agent_0_3_2_finder.py").write_text(
        f"import sys\nMAPPING: dict[str, str] = {{'unified_agent': {str(target)!r}}}\n",
        encoding="utf-8",
    )


def test_a_setuptools_finder_mapping_into_a_worktree_is_degraded(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    stale = repo / ".worktrees" / "task-5" / "src" / "unified_agent"
    _finder(repo / ".venv", stale)
    row = doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")
    assert row["status"] == "DEGRADED"
    assert str(stale) in row["detail"]


def test_a_setuptools_finder_mapping_into_main_is_live(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _finder(repo / ".venv", repo / "src" / "unified_agent")
    row = doctor._package_checkout_check(checkout=repo, prefix=repo / ".venv")
    assert row["status"] == "LIVE", row
