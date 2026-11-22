#!/usr/bin/env bash
# Rebuild the hermes-agent venv WITH the [mcp] optional extra.
#
# Background: hermes-agent fails open when the `mcp` package is missing — its
# MCP integration silently disappears instead of erroring. A venv rebuilt with
# a plain `pip install -e .` drops the extra and triggers exactly that. This
# wrapper lives outside the hermes checkout (no upstream drift), always
# installs the extra, and refuses to declare success until `import mcp` works.
#
# Usage:  hermes-venv-rebuild.sh [HERMES_DIR]
#   HERMES_DIR defaults to $HERMES_DIR if set, else ~/.hermes/hermes-agent.
#   A positional argument overrides both.
set -euo pipefail

hermes_dir=${1:-${HERMES_DIR:-$HOME/.hermes/hermes-agent}}
venv="$hermes_dir/venv"

# --- Sanity checks (nothing is removed until all of these pass) ---
if [ ! -d "$hermes_dir" ]; then
    echo "error: hermes directory not found: $hermes_dir" >&2
    exit 1
fi
if [ ! -f "$hermes_dir/pyproject.toml" ] && [ ! -f "$hermes_dir/setup.py" ]; then
    echo "error: $hermes_dir does not look like a Python project (no pyproject.toml or setup.py)" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi

# --- Rebuild ---
if [ -d "$venv" ]; then
    echo "Removing existing venv: $venv"
    rm -rf "$venv"
fi

echo "Creating venv: $venv"
python3 -m venv "$venv"
"$venv/bin/pip" install --upgrade pip
echo "Installing hermes-agent (editable) with the [mcp] extra"
"$venv/bin/pip" install -e "${hermes_dir}[mcp]"

# --- Verification gate: the whole point of this wrapper ---
if ! "$venv/bin/python" -c "import mcp" >/dev/null 2>&1; then
    echo "ERROR: 'import mcp' failed in the rebuilt venv." >&2
    echo "The [mcp] extra did not land — hermes will silently drop its MCP" >&2
    echo "integration (it fails open). Check that $hermes_dir declares an" >&2
    echo "[mcp] optional extra and rerun." >&2
    exit 1
fi

mcp_version=$("$venv/bin/python" -c "import importlib.metadata as m; print(m.version('mcp'))" 2>/dev/null || echo "unknown")
echo "OK: hermes venv rebuilt with [mcp] extra (mcp version: $mcp_version)"
