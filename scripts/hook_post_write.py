#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main_checkout(repo_root):
    """The primary checkout when `repo_root` is a linked git worktree, else None."""
    try:
        dotgit = repo_root / ".git"
        if not dotgit.is_file():
            return None
        text = dotgit.read_text().strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = repo_root / gitdir
        commondir = gitdir / "commondir"
        if commondir.is_file():
            common = (gitdir / commondir.read_text().strip()).resolve()
        else:
            common = gitdir.parent.parent.resolve()
        return common.parent
    except OSError:
        return None


def main():
    try:
        stdin_content = sys.stdin.read()

        if not stdin_content.strip():
            sys.exit(0)

        data = json.loads(stdin_content)

        # Look for file path in various potential places
        file_path = None

        # 1. toolCall structure
        tool_call = data.get("toolCall") or data.get("tool_call")
        if isinstance(tool_call, dict):
            args = tool_call.get("args") or tool_call.get("arguments") or {}
            if isinstance(args, dict):
                file_path = args.get("TargetFile") or args.get("file_path") or args.get("path")

        # 2. tool_input structure (Claude Code style)
        if not file_path:
            tool_input = data.get("tool_input") or data.get("toolInput") or {}
            if isinstance(tool_input, dict):
                file_path = (
                    tool_input.get("file_path")
                    or tool_input.get("TargetFile")
                    or tool_input.get("path")
                )

        # 3. Direct arguments or inputs in root
        if not file_path:
            file_path = data.get("TargetFile") or data.get("file_path") or data.get("path")

        if not file_path:
            sys.exit(0)

        file_path_obj = Path(file_path).resolve()

        if not file_path_obj.exists():
            sys.exit(0)

        if file_path_obj.suffix != ".py":
            sys.exit(0)

        # Run ruff check --fix
        repo_root = Path(__file__).parent.parent
        venv_ruff = repo_root / ".venv" / "bin" / "ruff"
        configured_ruff = os.environ.get("UA_RUFF_BIN", "").strip()
        if configured_ruff:
            venv_ruff = Path(configured_ruff).expanduser()
        elif not venv_ruff.is_file():
            # A linked worktree usually has no .venv of its own: use the main checkout's.
            main = main_checkout(repo_root)
            main_ruff = main / ".venv" / "bin" / "ruff" if main else None
            if main_ruff is not None and main_ruff.is_file():
                venv_ruff = main_ruff
            else:
                discovered = shutil.which("ruff")
                if not discovered:
                    return
                venv_ruff = Path(discovered)
        if not venv_ruff.is_file():
            return

        subprocess.run(
            [str(venv_ruff), "check", "--fix", str(file_path_obj)], capture_output=True, text=True
        )

    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
