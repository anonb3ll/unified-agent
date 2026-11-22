"""Safe local code quality checking and formatting tools using Ruff."""

import os
import subprocess
import sys

from mcp.server.mcpserver.exceptions import ToolError

from ..safety import resolve_in_workspace


def python_lint_and_format(path: str) -> dict:
    """Run local ruff checking, auto-fixing, and code formatting on a specified Python file or directory.

    Returns {lint_stdout, lint_stderr, format_stdout, format_stderr, success}.
    """
    from ..execution_locality import refuse_tool

    # A writable CWD can shadow `python -m ruff` with project code.
    refuse_tool("python_lint_and_format")
    resolved_path = resolve_in_workspace(path)
    if not resolved_path.exists():
        raise ToolError(f"Target path {path!r} does not exist.")

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }

    # 1. Run ruff check --fix
    try:
        res_lint = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--fix", str(resolved_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("Ruff lint check timed out after 30 seconds") from None
    except Exception as e:
        raise ToolError(f"Failed to execute ruff check: {e}") from e

    # 2. Run ruff format
    try:
        res_format = subprocess.run(
            [sys.executable, "-m", "ruff", "format", str(resolved_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("Ruff format timed out after 30 seconds") from None
    except Exception as e:
        raise ToolError(f"Failed to execute ruff format: {e}") from e

    success = (res_lint.returncode == 0) and (res_format.returncode == 0)

    return {
        "lint_stdout": res_lint.stdout.strip(),
        "lint_stderr": res_lint.stderr.strip(),
        "format_stdout": res_format.stdout.strip(),
        "format_stderr": res_format.stderr.strip(),
        "success": success,
    }
