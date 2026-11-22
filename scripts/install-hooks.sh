#!/usr/bin/env bash
# Install the repo git hooks: pre-commit security-audit gate plus the
# staged-conflict-marker guard (task #1016).
#
# .git/hooks is not version-controlled, so fresh clones silently lack the
# gate until this runs. Idempotent — safe to re-run after pulling.
#
# Usage:  ./scripts/install-hooks.sh
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
# `--git-path` (not a hardcoded "$repo_root/.git") is what makes this work
# from a worktree: inside a worktree `.git` is a *file* pointing at the real
# gitdir, and hooks live in the one common hooks dir shared by the main
# checkout and every worktree — same sharing hazard as the stash stack this
# task is fixing elsewhere. `--git-path` resolves to that shared dir from
# any of them.
hook="$(git rev-parse --git-path hooks)/pre-commit"
# A repository created without templates has no hooks directory yet.
mkdir -p "$(dirname "$hook")"

# The interpreter only supplies the audit's dependencies: the hook runs the
# committing checkout's own src through scripts/run_audit.py (task #2204), so
# a linked worktree is audited with its own code, not the code the shared
# venv's editable install points at. Prefer this checkout's venv, then the main
# checkout's (a worktree usually has none), then python3 on PATH.
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_root="$(dirname "$common_dir")"
python_bin="python3"
for candidate in "$repo_root/.venv/bin/python3" "$main_root/.venv/bin/python3"; do
  if [ -x "$candidate" ]; then
    python_bin="$candidate"
    break
  fi
done

# Because hooks are shared across worktrees but each worktree has its own
# working-tree path, the hook body resolves its OWN toplevel at run time
# (via `git rev-parse --show-toplevel`, run from whichever worktree is
# committing) rather than baking in the toplevel of whichever worktree
# happened to run install-hooks.sh. Only the fallback interpreter is baked in.
cat >"$hook" <<HOOK
#!/usr/bin/env bash
# Pre-commit gates. Installed by scripts/install-hooks.sh — edit there, not here.
set -e

commit_root="\$(git rev-parse --show-toplevel)"
py="\$commit_root/.venv/bin/python3"
[ -x "\$py" ] || py="$python_bin"

# Guard 1: block commits carrying merge-conflict markers in staged content
# (task #1016 — the exact signature a shared-stash pop collision leaves
# behind). See scripts/check_staged_conflict_markers.py.
"\$py" "\$commit_root/scripts/check_staged_conflict_markers.py"

# Guard 2: block commits carrying high-severity security-audit findings,
# audited with this checkout's own src (task #2204).
if [ -f "\$commit_root/scripts/run_audit.py" ]; then
  exec "\$py" "\$commit_root/scripts/run_audit.py" . --fail-on high
fi
# A branch from before scripts/run_audit.py existed: same import order.
export PYTHONPATH="\$commit_root/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$py" -m unified_agent.audit.cli . --fail-on high
HOOK

chmod +x "$hook"
echo "Installed pre-commit gates (conflict-marker guard + audit of the committing checkout's src) at $hook (fallback python=$python_bin)"
