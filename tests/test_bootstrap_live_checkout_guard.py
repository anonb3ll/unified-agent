"""bootstrap-server-dev.sh must not pull the live hub checkout (task #1771)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from unified_agent import hub_runtime

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy/mac-hub/bootstrap-server-dev.sh"
# The public export drops deploy/mac-hub/ (scripts/public_release/exclude.txt),
# so the exported tree cannot read the script: skip instead of failing collection.
# Gate the skip on being an export (#1597 review): a renamed script in the private
# repo must fail loudly here instead of skipping green.
PUBLIC_EXPORT = not (REPO / "scripts" / "public_release").is_dir()
if PUBLIC_EXPORT and not SCRIPT.exists():
    pytest.skip(f"{SCRIPT.name} is not in this tree", allow_module_level=True)
_TEXT = SCRIPT.read_text(encoding="utf-8")
_GUARD = re.search(r"^ua_repo_is_live_hub_checkout\(\) \{\n.*?^\}\n", _TEXT, re.M | re.S)


def _guard(home: Path, ua_repo: Path, ua_home: Path | None = None) -> int:
    assert _GUARD is not None, "guard function missing from bootstrap-server-dev.sh"
    env = {**os.environ, "HOME": str(home), "UA_REPO": str(ua_repo)}
    env.pop("UA_HOME", None)
    if ua_home is not None:
        env["UA_HOME"] = str(ua_home)
    script = _GUARD.group(0) + "ua_repo_is_live_hub_checkout\n"
    return subprocess.run(["bash", "-c", script], env=env, check=False).returncode


@pytest.fixture
def mac_home(tmp_path):
    home = tmp_path / "home"
    (home / "dev" / "unified_agent").mkdir(parents=True)
    return home


def test_not_live_on_a_fresh_host(mac_home):
    assert _guard(mac_home, mac_home / "dev" / "unified_agent") == 1


def test_live_when_hub_launchagent_runs_from_the_checkout(mac_home):
    repo = mac_home / "dev" / "unified_agent"
    agents = mac_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.unified-agent.hub.plist").write_text(
        f"<string>{repo}/deploy/mac-hub/run-hub.sh</string>"
    )
    assert _guard(mac_home, repo) == 0


def test_hub_launchagent_for_another_checkout_does_not_block(mac_home):
    agents = mac_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.unified-agent.hub.plist").write_text(
        "<string>/elsewhere/unified_agent/deploy/mac-hub/run-hub.sh</string>"
    )
    assert _guard(mac_home, mac_home / "dev" / "unified_agent") == 1


def test_live_when_hub_start_record_names_the_checkout(mac_home, monkeypatch):
    """The marker the hub writes at startup is the one the script reads."""
    repo = (mac_home / "dev" / "unified_agent").resolve()
    ua_home = mac_home / ".unified_agent"
    monkeypatch.setenv("UA_HOME", str(ua_home))
    assert hub_runtime.record_hub_start(root=repo) is not None
    assert hub_runtime.start_record_path().parent == ua_home
    assert _guard(mac_home, repo) == 0  # default UA_HOME under $HOME
    assert _guard(mac_home, repo, ua_home=ua_home) == 0


def test_pull_runs_only_when_the_guard_says_not_live():
    pulls = [m.start() for m in re.finditer(r'git -C "\$UA_REPO" pull', _TEXT)]
    assert len(pulls) == 1
    guard_branch = _TEXT.index('if [[ -e "$UA_REPO" ]] && ua_repo_is_live_hub_checkout; then')
    else_branch = _TEXT.index("else", guard_branch)
    assert guard_branch < else_branch < pulls[0]
    # The guard also precedes the branch that deletes a partial checkout.
    assert guard_branch < _TEXT.index('rm -rf "$UA_REPO"')


# The whole clone/update step, run with a stub ``git`` so its branches are
# exercised for real (review: a linked worktree has a ``.git`` *file*).
_STEP = re.search(
    r"^ua_repo_is_live_hub_checkout\(\) \{\n.*?(?=^log \"uv sync in unified_agent\")",
    _TEXT,
    re.M | re.S,
)


def _run_step(home: Path, ua_repo: Path, tmp_path: Path) -> list[str]:
    assert _STEP is not None, "clone/update step missing from bootstrap-server-dev.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "git-calls"
    stub = bin_dir / "git"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {calls}\n'
        'if [[ "$1" == clone ]]; then mkdir -p "${@: -1}/.git" "${@: -1}/deploy"; fi\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "UA_REPO": str(ua_repo),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    env.pop("UA_HOME", None)
    script = "log() { :; }\nwarn() { :; }\n" + _STEP.group(0)
    subprocess.run(["bash", "-c", script], env=env, check=True)
    return calls.read_text().splitlines() if calls.exists() else []


def _linked_worktree(repo: Path) -> Path:
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/unified_agent\n")
    edit = repo / "uncommitted.py"
    edit.write_text("local edit\n")
    return edit


def _install_hub_plist(home: Path, repo: Path) -> None:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "com.unified-agent.hub.plist").write_text(
        f"<string>{repo}/deploy/mac-hub/run-hub.sh</string>"
    )


def test_live_linked_worktree_is_left_untouched(mac_home, tmp_path):
    repo = mac_home / "dev" / "unified_agent"
    edit = _linked_worktree(repo)
    _install_hub_plist(mac_home, repo)
    assert _run_step(mac_home, repo, tmp_path) == []
    assert edit.read_text() == "local edit\n"


def test_idle_linked_worktree_is_pulled_not_replaced(mac_home, tmp_path):
    repo = mac_home / "dev" / "unified_agent"
    edit = _linked_worktree(repo)
    calls = _run_step(mac_home, repo, tmp_path)
    assert calls == [f"-C {repo} pull --ff-only"]
    assert edit.read_text() == "local edit\n"


def test_live_partial_checkout_is_not_replaced(mac_home, tmp_path):
    repo = mac_home / "dev" / "unified_agent"
    served = repo / "deploy" / "mac-hub" / "run-hub.sh"
    served.parent.mkdir(parents=True)
    served.write_text("#!/bin/sh\n")
    _install_hub_plist(mac_home, repo)
    assert _run_step(mac_home, repo, tmp_path) == []
    assert served.exists()


def test_missing_checkout_is_cloned_even_with_a_stale_hub_plist(mac_home, tmp_path):
    repo = mac_home / "dev" / "unified_agent"
    repo.rmdir()
    _install_hub_plist(mac_home, repo)
    calls = _run_step(mac_home, repo, tmp_path)
    assert calls == [f"clone https://github.com/anonb3ll/unified-agent.git {repo}"]
