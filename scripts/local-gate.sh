#!/usr/bin/env bash
# Local verification gate — a repo-owned, single-command mirror of the CI
# `test` and `gates` jobs in .github/workflows/ci.yml, run against the exact SHA (or
# working tree — see "Attribution" below) in your worktree. (task #1214)
#
# GitHub Actions is NOT paused for billing (verified via `gh run list`: the
# last CI run completed successfully on main at 2026-09-19T23:14Z). This is a
# *pre-push* gate — run it before `git push` to catch the same failures CI
# would, before spending CI minutes on them — and it doubles as a substitute
# if Actions is ever unavailable. It never changes what CI does; it only
# reproduces it locally.
#
# CI-mirror legs (same commands as .github/workflows/ci.yml's `test` job —
# pytest — and `gates` job — sync, lint, format, smoke, audit, named gates —
# with a local lock-preserving guard on dependency sync — each one runs
# independently; a failing leg does not stop the rest):
#   1. uv sync --all-extras --locked
#   2. uv run ruff check .
#   3. uv run ruff format --check .
#   3b. uv run python scripts/check_layers.py   (layer map, docs/architecture/layers.md)
#   3c. uv run python scripts/changelog_assemble.py --check
#   4. uv run pytest -q                          (full suite by default)
#   5. make smoke
#   6. uv run unified-agent-audit . --fail-on high
#   7. uv run pytest tests/test_attribution_coverage.py -q          (named gate)
#   8. uv run pytest tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture -q  (named gate)
#
# Local-only legs (not in CI's `test` job, kept separate so a skip/fail here
# never reads as a CI-parity gap):
#   8. scripts/check_staged_conflict_markers.py   (staged conflict-marker guard, #1016)
#   9. uv run python scripts/check_components.py   (component map,
#      docs/architecture/components.md; joins CI's `gates` job with #2440)
#
# Attribution: if the worktree is dirty, this run cannot certify the commit
# at HEAD — it necessarily also exercises your uncommitted changes, which
# `git push` will NOT send. In that case the gate refuses to attribute PASS
# to the HEAD SHA; the summary instead reports the result against the
# WORKING TREE (printed as "<sha>+dirty") and says so plainly. Commit (or
# stash) before running the gate if you need a result attributable to a
# specific commit.
#
# Usage:
#   scripts/local-gate.sh                 # full gate (full pytest — ~31 min on a 4-core desktop)
#   scripts/local-gate.sh --fast          # skip the full pytest suite; every other leg still runs
#   scripts/local-gate.sh --targeted "tests/test_foo.py -k 'failed login'"
#                                          # shell-style quoting is parsed into literal pytest argv
#   scripts/local-gate.sh --affected      # pytest on the test files this branch's diff (vs
#                                          # origin/main, plus uncommitted changes) can affect,
#                                          # from scripts/affected_tests.py (task #2231). When the
#                                          # diff needs the whole suite (pyproject.toml, uv.lock,
#                                          # tests/conftest.py, an unmapped file) the pytest leg is
#                                          # SKIPPED with the reason: CI runs the full suite.
#
# Tradeoff: --fast, --targeted and --affected give you a quick sanity check on everything
# BUT test coverage — they are not a substitute for a green CI run, which
# always runs the full suite on the PR. Use the default (no flags) mode
# before a push you can't watch CI finish for, or when you've touched
# anything with cross-module blast radius.
#
# Exit status: non-zero if any leg fails, including a requested pytest leg
# (full or --targeted) that never ran (#2027). A single PASS/FAIL summary table
# and the verified attribution (commit or working tree) print at the end
# regardless of outcome.
set -uo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

MODE="full"
TARGETED_ARGS=""

usage() {
  awk '/^# Local verification gate/{p=1} p{print} /^set -uo pipefail/{exit}' "$0" \
    | sed '$d' \
    | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fast)
      MODE="fast"
      shift
      ;;
    --affected)
      MODE="affected"
      shift
      ;;
    --targeted)
      TARGETED_ARGS="${2:-}"
      if [[ -z "$TARGETED_ARGS" ]]; then
        echo "error: --targeted requires an argument (e.g. --targeted \"tests/test_foo.py -k bar\")" >&2
        exit 2
      fi
      MODE="targeted"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unrecognized argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# ---------------------------------------------------------------------------
# Header: the exact tree this run verified, so results are attributable.
# A dirty worktree cannot be attributed to head_sha (see "Attribution" above)
# — attribution_label carries whichever is actually true, and every later
# message (header + summary + final verdict) uses it instead of head_sha.
# ---------------------------------------------------------------------------
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
head_sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
  tree_state="clean"
  attribution_label="$head_sha"
  attribution_note="commit $head_sha"
else
  staged_n="$(git diff --cached --name-only 2>/dev/null | grep -c . || true)"
  unstaged_n="$(git diff --name-only 2>/dev/null | grep -c . || true)"
  untracked_n="$(git status --porcelain 2>/dev/null | grep -c '^??' || true)"
  tree_state="dirty (staged=${staged_n} unstaged=${unstaged_n} untracked=${untracked_n})"
  attribution_label="${head_sha}+dirty"
  attribution_note="the WORKING TREE (${head_sha}+dirty) — NOT commit ${head_sha} itself; uncommitted changes are exercised here but 'git push' will not send them. Commit or stash, then re-run, for a result attributable to a specific commit."
fi
run_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

case "$MODE" in
  full) mode_desc="full (full pytest suite)" ;;
  fast) mode_desc="fast (pytest suite skipped)" ;;
  targeted) mode_desc="targeted (pytest: ${TARGETED_ARGS})" ;;
  affected) mode_desc="affected (pytest: test files selected from the import graph)" ;;
esac

echo "==================================================================="
echo "unified-agent local verification gate (task #1214)"
echo "==================================================================="
echo "repo root   : $repo_root"
echo "branch      : $branch"
echo "HEAD SHA    : $head_sha"
echo "tree        : $tree_state"
echo "attribution : $attribution_note"
echo "mode        : $mode_desc"
echo "started     : $run_started_at"
echo "==================================================================="
echo

# ---------------------------------------------------------------------------
# Leg runner: records name, pass/fail, duration, and tail of output.
#
# Commands run as an argv array (no intermediate shell), so a --targeted
# value is passed straight to the underlying program's argument
# list rather than being re-parsed by a shell — a value containing shell
# metacharacters (';', '$()', backticks, ...) is inert here.
# ---------------------------------------------------------------------------
declare -a LEG_NAMES=()
declare -a LEG_STATUS=()
declare -a LEG_DURATION=()
declare -a LEG_DETAIL=()
OVERALL=0

run_leg() {
  local name="$1"
  shift
  # Display names may contain arbitrarily long --targeted selections. Use
  # the leg index for a short, unique filename and retain the full name below.
  local logfile="$workdir/leg-${#LEG_NAMES[@]}.log"
  echo "--- leg: $name ---"
  printf '$ '
  printf '%q ' "$@"
  echo
  local start end dur rc
  start="$(date +%s)"
  # "$@" is executed directly (no bash -c / no shell re-parse of the args).
  "$@" > "$logfile" 2>&1
  rc=$?
  end="$(date +%s)"
  dur=$((end - start))
  cat "$logfile"
  LEG_NAMES+=("$name")
  LEG_DURATION+=("${dur}s")
  if [[ $rc -eq 0 ]]; then
    LEG_STATUS+=("PASS")
    LEG_DETAIL+=("")
    echo "--- leg: $name -> PASS (${dur}s) ---"
  else
    LEG_STATUS+=("FAIL")
    LEG_DETAIL+=("exit $rc — see output above")
    OVERALL=1
    echo "--- leg: $name -> FAIL (exit $rc, ${dur}s) ---"
  fi
  echo
}

skip_leg() {
  local name="$1"
  local reason="$2"
  LEG_NAMES+=("$name")
  LEG_STATUS+=("SKIP")
  LEG_DURATION+=("0s")
  LEG_DETAIL+=("$reason")
  echo "--- leg: $name -> SKIP ($reason) ---"
  echo
}

# ===========================================================================
# CI-mirror legs — command-for-command match to the `test` job in
# .github/workflows/ci.yml (lines ~88-119 as of task #1214).
# ===========================================================================

# ---------------------------------------------------------------------------
# Leg: dependency installation (CI's first test-job step, with a local
# --locked guard so dependency setup cannot rewrite uv.lock after attribution)
# ---------------------------------------------------------------------------
run_leg "install dependencies" uv sync --all-extras --locked

# ---------------------------------------------------------------------------
# Leg: ruff check (repo-wide, matching CI's `ruff check .` — not the narrower
# `ruff check src tests` this script ran before task #1214's review pass;
# root-level files like scripts/smoke.py are lint-checked by CI and must be
# here too).
# ---------------------------------------------------------------------------
run_leg "ruff check ." uv run ruff check .

# ---------------------------------------------------------------------------
# Leg: ruff format --check (CI fails a push whose formatting drifted even
# when `ruff check` itself is silent; this repo's local gate silently didn't
# run this at all before task #1214's review pass).
# ---------------------------------------------------------------------------
run_leg "ruff format --check ." uv run ruff format --check .

# ---------------------------------------------------------------------------
# Leg: layer check (imports must not point up the layer map; see
# docs/architecture/layers.md). Matches CI's `gates` job step.
# ---------------------------------------------------------------------------
run_leg "layer check" uv run python scripts/check_layers.py

# ---------------------------------------------------------------------------
# Leg: component check (imports must follow the component map's depends_on;
# see docs/architecture/components.md). CI gains this step with task #2440.
# ---------------------------------------------------------------------------
run_leg "component check" uv run python scripts/check_components.py

# Validate fragments even when scoped pytest does not select the changelog tests.
run_leg "changelog fragments" uv run python scripts/changelog_assemble.py --check

# ---------------------------------------------------------------------------
# Leg: pytest
#
# Portability (task #2027): macOS /bin/bash is 3.2. Keep this block free of
# bash-4 builtins such as `mapfile`. Under `set -u` without `set -e`, bash
# reports an error like an unbound variable and then abandons the rest of the
# enclosing compound command (this whole `case`) while the script carries on.
# That is how a missing `mapfile` once let --targeted print PASS without ever
# running pytest. PYTEST_LEG_RAN is set only after the leg is recorded, and
# the check after this block fails the gate when a requested leg never ran.
# ---------------------------------------------------------------------------
PYTEST_LEG_RAN=0
if [[ "$MODE" == "targeted" ]]; then
  pytest_leg_name="pytest (targeted: ${TARGETED_ARGS})"
elif [[ "$MODE" == "affected" ]]; then
  pytest_leg_name="pytest (affected)"
else
  pytest_leg_name="pytest (full suite)"
fi
case "$MODE" in
  full)
    run_leg "$pytest_leg_name" uv run pytest -q
    PYTEST_LEG_RAN=1
    ;;
  targeted)
    # Parse shell-style quoting without eval, bash -c, or pathname expansion.
    # NUL-delimiting keeps each parsed item a literal argv element.
    if ! uv run python -c 'import os, shlex, sys; sys.stdout.buffer.write(b"".join(os.fsencode(arg) + b"\0" for arg in shlex.split(sys.argv[1])))' "$TARGETED_ARGS" > "$workdir/targeted-argv" 2> "$workdir/targeted-argv.err"; then
      echo "error: --targeted must contain valid shell-style quoting" >&2
      cat "$workdir/targeted-argv.err" >&2
      exit 2
    fi
    # bash 3.2-compatible NUL-delimited read (no `mapfile -d ''`, bash 4.4+).
    TARGETED_ARR=()
    while IFS= read -r -d '' targeted_item; do
      TARGETED_ARR+=("$targeted_item")
    done < "$workdir/targeted-argv"
    if [[ ${#TARGETED_ARR[@]} -eq 0 ]]; then
      echo "error: --targeted requires at least one pytest argument" >&2
      exit 2
    fi
    run_leg "$pytest_leg_name" uv run pytest -q "${TARGETED_ARR[@]}"
    PYTEST_LEG_RAN=1
    ;;
  affected)
    # scripts/affected_tests.py prints test files, nothing, or the sentinel ALL.
    if ! uv run python scripts/affected_tests.py --explain > "$workdir/affected" 2> "$workdir/affected.err"; then
      cat "$workdir/affected.err"
      LEG_NAMES+=("$pytest_leg_name")
      LEG_STATUS+=("FAIL")
      LEG_DURATION+=("0s")
      LEG_DETAIL+=("test selection failed — see scripts/affected_tests.py output above")
      OVERALL=1
      echo "--- leg: $pytest_leg_name -> FAIL (test selection failed) ---"
      echo
    else
      cat "$workdir/affected.err"
      # bash 3.2-compatible line read (no mapfile).
      AFFECTED_ARR=()
      while IFS= read -r affected_item || [[ -n "$affected_item" ]]; do
        if [[ -n "$affected_item" ]]; then
          AFFECTED_ARR+=("$affected_item")
        fi
      done < "$workdir/affected"
      if [[ ${#AFFECTED_ARR[@]} -eq 1 && "${AFFECTED_ARR[0]}" == "ALL" ]]; then
        skip_leg "$pytest_leg_name" "this diff needs the full suite (reason above); CI runs it: label the PR ci:full or let the merge batch verify it"
      elif [[ ${#AFFECTED_ARR[@]} -eq 0 ]]; then
        skip_leg "$pytest_leg_name" "no test files are affected by this diff"
      else
        run_leg "pytest (affected: ${#AFFECTED_ARR[@]} files)" uv run pytest -q "${AFFECTED_ARR[@]}"
      fi
    fi
    PYTEST_LEG_RAN=1
    ;;
  fast)
    skip_leg "pytest" "skipped by --fast — full suite still runs in CI on the PR; not a substitute for green CI"
    PYTEST_LEG_RAN=1
    ;;
esac
if [[ "$PYTEST_LEG_RAN" != 1 ]]; then
  # The requested pytest leg was never recorded: fail it explicitly so the
  # summary shows it and the gate cannot report PASS without it.
  LEG_NAMES+=("$pytest_leg_name")
  LEG_STATUS+=("FAIL")
  LEG_DURATION+=("0s")
  LEG_DETAIL+=("did not run — the pytest step aborted; see the bash error above")
  OVERALL=1
  echo "--- leg: $pytest_leg_name -> FAIL (did not run; the pytest step aborted, see the bash error above) ---"
  echo
fi

# ---------------------------------------------------------------------------
# Leg: smoke (unmocked real-dependency checks, CI's `make smoke` step)
# ---------------------------------------------------------------------------
run_leg "smoke (make smoke)" make smoke

# ---------------------------------------------------------------------------
# Leg: security audit gate
#
# `unified-agent-audit . --fail-on high` reports status "warn" when only
# medium/low/info findings exist (this repo carries 2 pre-existing medium
# findings as of task #1214 — INJECTION-BASE64 in two docs/designs/*.md
# files). That is exit 0 and a PASS here, by design: --fail-on high means
# exactly that, only high-severity findings fail the gate.
# ---------------------------------------------------------------------------
run_leg "security audit (--fail-on high)" uv run unified-agent-audit . --fail-on high

# ---------------------------------------------------------------------------
# Leg: named gate — attribution coverage (re-asserted by CI by name; the
# full pytest run above already covers it, but CI treats it as
# independently load-bearing, so this gate does too).
# ---------------------------------------------------------------------------
run_leg "named gate: attribution coverage" uv run pytest tests/test_attribution_coverage.py -q

# ---------------------------------------------------------------------------
# Leg: named gate — audit ledger tamper fixture (same rationale).
# ---------------------------------------------------------------------------
run_leg "named gate: audit ledger tamper fixture" uv run pytest tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture -q

# ===========================================================================
# Local-only legs — not part of CI's `test` job, kept separate from the
# CI-mirror legs above so their pass/skip/fail never reads as CI-parity
# information.
# ===========================================================================

# ---------------------------------------------------------------------------
# Local-only leg: staged conflict-marker guard (task #1016)
#
# Checks currently-STAGED content, same as the pre-commit hook installed by
# scripts/install-hooks.sh. If nothing is staged (the common case right
# before a push, after everything is already committed) this trivially
# passes — it exists to catch an unresolved `git stash pop` collision before
# it is committed, not to re-scan history.
# ---------------------------------------------------------------------------
python_bin="python3"
if [[ -x "$repo_root/.venv/bin/python3" ]]; then
  python_bin="$repo_root/.venv/bin/python3"
fi
run_leg "local-only: staged conflict-marker guard" "$python_bin" scripts/check_staged_conflict_markers.py

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "==================================================================="
echo "SUMMARY — verified ${attribution_note}"
echo "on $branch ($tree_state)"
echo "==================================================================="
printf "%-42s %-6s %s\n" "LEG" "RESULT" "DETAIL"
printf "%-42s %-6s %s\n" "---" "------" "------"
for i in "${!LEG_NAMES[@]}"; do
  printf "%-42s %-6s %s\n" "${LEG_NAMES[$i]} (${LEG_DURATION[$i]})" "${LEG_STATUS[$i]}" "${LEG_DETAIL[$i]}"
done
echo "==================================================================="
if [[ "$tree_state" == "clean" ]]; then
  if [[ $OVERALL -eq 0 ]]; then
    echo "GATE RESULT: PASS — commit $head_sha ($branch) is clean to push"
  else
    echo "GATE RESULT: FAIL — commit $head_sha ($branch) has failing legs above; fix before pushing"
  fi
else
  if [[ $OVERALL -eq 0 ]]; then
    echo "GATE RESULT: PASS (WORKING TREE ONLY) — ${head_sha}+dirty on $branch passed every leg,"
    echo "but this is NOT a verified result for commit $head_sha: your worktree is dirty, so"
    echo "'git push' would send a commit this run did not test. Commit/stash, then re-run, before pushing."
  else
    echo "GATE RESULT: FAIL (WORKING TREE) — ${head_sha}+dirty on $branch has failing legs above;"
    echo "fix before committing. (Worktree is dirty, so this result is not attributable to commit $head_sha.)"
  fi
fi
echo "==================================================================="

# ---------------------------------------------------------------------------
# Receipts (task #2412, report mode): what each leg proved about this tree.
# A receipt names a tree, so a dirty run records nothing, and failing to
# record never changes the gate's result.
# ---------------------------------------------------------------------------
if [[ "$tree_state" == "clean" && -f scripts/gate_route.py ]]; then
  for i in "${!LEG_NAMES[@]}"; do
    case "${LEG_STATUS[$i]}" in
      PASS) receipt_conclusion="success" ;;
      FAIL) receipt_conclusion="failure" ;;
      *) receipt_conclusion="skipped" ;;
    esac
    case "${LEG_NAMES[$i]}" in
      "pytest (full suite)") receipt_scope="full" ;;
      *) receipt_scope="partial" ;;
    esac
    "$python_bin" scripts/gate_route.py record --check "${LEG_NAMES[$i]}" \
      --conclusion "$receipt_conclusion" --scope "$receipt_scope" --head "$head_sha" \
      > /dev/null 2>&1 || true
  done
fi

exit $OVERALL
