"""Antigravity hook scripts resolve their tools from a worktree without its own `.venv`."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"


def _stub(path: Path, exit_code: int = 0) -> Path:
    """A fake CLI that records its invocation next to itself and exits `exit_code`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$@" >> "$0.calls"\nexit {exit_code}\n')
    path.chmod(0o755)
    return path


def _calls(stub: Path) -> list[str]:
    log = Path(f"{stub}.calls")
    return log.read_text().splitlines() if log.exists() else []


def _checkout(root: Path) -> Path:
    """A checkout holding copies of the hook scripts (and nothing else)."""
    (root / "scripts").mkdir(parents=True)
    for name in ("hook_pre_push.py", "hook_post_write.py", "run_audit.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    return root


def _worktree_of(main: Path, worktree: Path) -> Path:
    """Wire `worktree` to `main` the way `git worktree add` does, without running git."""
    gitdir = main / ".git" / "worktrees" / worktree.name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    return _checkout(worktree)


def _run(script: Path, payload: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    bare_bin = tmp_path / "empty-bin"  # a PATH with no ruff / python3 on it
    bare_bin.mkdir(exist_ok=True)
    env = {"PATH": str(bare_bin), "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=script.parent.parent,
        timeout=60,
    )


def _push(checkout: Path) -> dict:
    return {"tool_input": {"command": "git push origin HEAD", "cwd": str(checkout)}}


def _tree_probe(path: Path) -> Path:
    """A fake CLI that records its cwd, a `marker.txt` in that cwd, and its argv.

    The copy uses shell builtins: the hook's PATH has no `cat`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'pwd > "$0.cwd"\n'
        "if [ -f marker.txt ]; then\n"
        '  while IFS= read -r line || [ -n "$line" ]; do\n'
        "    printf '%s\\n' \"$line\"\n"
        '  done < marker.txt > "$0.marker"\n'
        "else\n"
        '  echo MISSING > "$0.marker"\n'
        "fi\n"
        'echo "$@" >> "$0.calls"\n'
        "exit 0\n"
    )
    path.chmod(0o755)
    return path


def _side(stub: Path, suffix: str) -> str:
    return Path(f"{stub}.{suffix}").read_text()


def _load_hook(path: Path):
    spec = importlib.util.spec_from_file_location("hook_pre_push_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pre_push_blocks_when_no_check_tool_can_be_found(tmp_path):
    wt = _checkout(tmp_path / "wt")
    res = _run(wt / "scripts" / "hook_pre_push.py", _push(wt), tmp_path)
    decision = json.loads(res.stdout)
    assert decision["decision"] == "block", res.stdout
    assert "ruff" in decision["reason"]


def test_pre_push_in_worktree_runs_the_main_checkout_tools(tmp_path):
    main = _checkout(tmp_path / "main")
    ruff = _stub(main / ".venv" / "bin" / "ruff")
    python = _stub(main / ".venv" / "bin" / "python3")
    wt = _worktree_of(main, tmp_path / "wt")
    res = _run(wt / "scripts" / "hook_pre_push.py", _push(wt), tmp_path)
    assert json.loads(res.stdout) == {"decision": "allow"}, res.stdout
    assert _calls(ruff) == ["check"]
    # The main checkout's interpreter runs the WORKTREE's audit entry point,
    # which imports the worktree's own src (task #2204).
    assert _calls(python) == [f"{wt / 'scripts' / 'run_audit.py'} . --fail-on high"]


def test_pre_push_in_worktree_blocks_when_a_main_checkout_check_fails(tmp_path):
    main = _checkout(tmp_path / "main")
    _stub(main / ".venv" / "bin" / "ruff", exit_code=1)
    _stub(main / ".venv" / "bin" / "python3")
    wt = _worktree_of(main, tmp_path / "wt")
    res = _run(wt / "scripts" / "hook_pre_push.py", _push(wt), tmp_path)
    assert json.loads(res.stdout)["decision"] == "block", res.stdout


def test_pre_push_prefers_the_worktrees_own_venv(tmp_path):
    main = _checkout(tmp_path / "main")
    main_ruff = _stub(main / ".venv" / "bin" / "ruff")
    _stub(main / ".venv" / "bin" / "python3")
    wt = _worktree_of(main, tmp_path / "wt")
    own_ruff = _stub(wt / ".venv" / "bin" / "ruff")
    _stub(wt / ".venv" / "bin" / "python3")
    res = _run(wt / "scripts" / "hook_pre_push.py", _push(wt), tmp_path)
    assert json.loads(res.stdout) == {"decision": "allow"}, res.stdout
    assert _calls(own_ruff) == ["check"]
    assert _calls(main_ruff) == []


def test_pre_push_from_task_worktree_reads_that_worktree(tmp_path):
    """A push from `.worktrees/task-<id>` is gated on that tree, not the hook's checkout."""
    main = _checkout(tmp_path / "repo")
    (main / "marker.txt").write_text("hook-checkout\n")
    ruff = _tree_probe(main / ".venv" / "bin" / "ruff")
    python = _tree_probe(main / ".venv" / "bin" / "python3")
    worktree = _worktree_of(main, main / ".worktrees" / "task-42")
    (worktree / "marker.txt").write_text("worktree\n")

    res = _run(main / "scripts" / "hook_pre_push.py", _push(worktree), tmp_path)

    assert json.loads(res.stdout)["decision"] == "allow", res.stdout
    assert Path(_side(ruff, "cwd").strip()) == worktree.resolve()
    assert _side(ruff, "marker") == "worktree\n"
    assert Path(_side(python, "cwd").strip()) == worktree.resolve()
    assert _side(python, "marker") == "worktree\n"
    assert _calls(python) == [f"{worktree / 'scripts' / 'run_audit.py'} . --fail-on high"]


def test_pre_push_blocks_when_a_check_raises(tmp_path, monkeypatch, capsys):
    main = _checkout(tmp_path / "repo")
    _stub(main / ".venv" / "bin" / "ruff")
    _stub(main / ".venv" / "bin" / "python3")
    hook = _load_hook(main / "scripts" / "hook_pre_push.py")

    def explode(*args, **kwargs):
        raise RuntimeError("ruff check crashed")

    monkeypatch.setattr(hook.subprocess, "run", explode)
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(_push(main))))
    with pytest.raises(SystemExit) as caught:
        hook.main()
    assert caught.value.code == 1
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "ruff check crashed" in decision["reason"]


def test_post_write_in_worktree_fixes_with_the_main_checkout_ruff(tmp_path):
    main = _checkout(tmp_path / "main")
    ruff = _stub(main / ".venv" / "bin" / "ruff")
    wt = _worktree_of(main, tmp_path / "wt")
    source = wt / "sample.py"
    source.write_text("x = 1\n")
    payload = {"tool_input": {"file_path": str(source)}}
    res = _run(wt / "scripts" / "hook_post_write.py", payload, tmp_path)
    assert res.returncode == 0
    assert _calls(ruff) == [f"check --fix {source.resolve()}"]


@needs_exported_paths(ROOT / ".agents" / "hooks.json", repo=ROOT)
def test_hooks_json_commands_resolve_paths_relative_to_the_checkout():
    hooks = json.loads((ROOT / ".agents" / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for group in hooks.values() for g in group for h in g["hooks"]]
    assert commands and all("../scripts/hook_" in c for c in commands)
    assert not any(os.path.isabs(tok) for c in commands for tok in c.split())
