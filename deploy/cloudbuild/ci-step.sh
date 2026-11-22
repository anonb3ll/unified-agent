#!/usr/bin/env bash
# One CI step inside a privileged Cloud Build container (task #2115).
#
#   ci-step.sh setup | gates | shard N | js
#
# The commit under test is extracted at /workspace/repo; CI scratch lives in
# /workspace/.ci, outside the tree the checks lint and audit.
#
# Mirrors the `gates`, `test (shard=N)` and `js` jobs of
# .github/workflows/ci.yml. Every step shares /workspace, so `setup` builds the
# venv once and the other steps reuse it. The containers run --privileged,
# which bubblewrap needs for its user namespace (the PDF worker and sandboxed
# CLI spawns fail closed without it).
#
# Root is used only to install system packages and mount the pytest tmpfs. The
# checks themselves run as an unprivileged `ci` user (uid 1000), as on a GitHub
# runner: permission-sensitive tests mean nothing as root.
set -euo pipefail

UV_VERSION=0.11.32
CI_UID=1000

if [ "$(id -u)" = "0" ]; then
    step="${1:-}"
    if [ "$step" != "native" ]; then
        if ! command -v bwrap >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
            apt-get update -qq >/dev/null
            apt-get install -y -qq --no-install-recommends bubblewrap make git ca-certificates curl >/dev/null
        fi
    fi
    if [ "$step" = "js" ]; then
        # Browser libraries need root; browser download and tests remain uid 1000.
        playwright_version="$(node -p "require('/workspace/repo/packages/web/package-lock.json').packages['node_modules/@playwright/test'].version")"
        npx --yes "playwright@$playwright_version" install-deps chromium
    fi
    if [ "$step" = "native" ]; then
        rustup component add rustfmt clippy
    fi
    # uid 1000 owns /workspace in every image; node:22 already names it `node`.
    ci_user="$(getent passwd "$CI_UID" | cut -d: -f1 || true)"
    if [ -z "$ci_user" ]; then
        useradd --uid "$CI_UID" --create-home --shell /bin/bash ci
        ci_user=ci
    fi
    ci_home="$(getent passwd "$ci_user" | cut -d: -f6)"
    if [ "$step" = "setup" ]; then
        chown -R "$CI_UID:$CI_UID" /workspace
    fi
    # Same reason as CI's tmpfs step: SQLite fsync dominates on disk (#1628).
    mkdir -p /tmp/pytest-tmpfs
    mount -t tmpfs -o size=4g,mode=1777 tmpfs /tmp/pytest-tmpfs 2>/dev/null || chmod 1777 /tmp/pytest-tmpfs
    exec runuser -u "$ci_user" -- env HOME="$ci_home" PATH="$PATH" bash "$0" "$@"
fi

cd /workspace/repo
export UV_CACHE_DIR=/workspace/.ci/uv-cache
export UV_LINK_MODE=copy
export PATH="/workspace/.ci/bin:$PATH"
export HF_HOME=/workspace/.ci/hf
export CARGO_HOME=/workspace/.ci/cargo
export RUSTUP_HOME=/usr/local/rustup
export TMPDIR=/tmp/pytest-tmpfs

ensure_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        mkdir -p /workspace/.ci/bin
        curl -fLsS "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-$(uname -m)-unknown-linux-gnu.tar.gz" \
            | tar -xz -C /workspace/.ci/bin --strip-components=1
    fi
}

step="${1:?usage: ci-step.sh setup|gates|shard N|js}"
if [ "$step" != "setup" ]; then
    # setup owns the shared environment. Parallel jobs must not resync it.
    export UV_NO_SYNC=1
fi
case "$step" in
setup)
    mkdir -p /workspace/.ci
    test "$(git rev-parse HEAD)" = "${UA_CI_SOURCE_SHA:?exact source SHA required}"
    # The build independently classifies the two submitted shallow trees;
    # caller substitutions cannot silently switch off a required job.
    git diff --name-only refs/ci/base HEAD | python3 -I scripts/ci_changes.py classify \
        > /workspace/.ci/classification
    git diff --name-only refs/ci/base HEAD | python3 -I scripts/ci_changes.py exhaustive \
        > /workspace/.ci/exhaustive
    ensure_uv
    uv sync --locked --all-extras
    bwrap --ro-bind / / true && echo "bwrap usable as $(id -un)" \
        || echo "WARNING: bwrap unusable; sandboxed tests will fail closed"
    # Git-dependent checks need the real shallow commit. Parallel jobs get
    # private metadata and source files; only the completed venv is shared.
    for job in gates shard-0 shard-1 shard-2 shard-3 js native; do
        checkout="/workspace/.ci/checkouts/$job"
        mkdir -p "$checkout"
        tar -C /workspace/repo --exclude=./.venv -cf - . | tar -C "$checkout" -xf -
    done
    ;;
gates)
    uv run --frozen ruff check .
    uv run --frozen ruff format --check .
    uv run --frozen python scripts/check_layers.py
    python3 scripts/changelog_assemble.py --check
    # Per-change test budget (task #2233). Only `scripts/cloud-ci.sh <pr>`
    # uploads its inputs (refs/ci/budget-base and the PR's commit messages);
    # batch, branch and SHA runs skip it, as GitHub Actions does outside
    # pull_request events. A missing base or unreadable input fails (exit 2).
    if [ -f /workspace/commit-messages ] && [ -f scripts/test_budget.py ]; then
        python3 -I scripts/test_budget.py count --base refs/ci/budget-base --head HEAD \
            --messages /workspace/commit-messages --messages-format nul
    else
        echo "test budget: skipped (not a pull request run, or the head has no scripts/test_budget.py)"
    fi
    make smoke
    uv run --frozen unified-agent-audit . --fail-on high
    uv run --frozen pytest tests/test_attribution_coverage.py -q
    uv run --frozen pytest tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture -q
    uv run --frozen pytest tests/connectors -q
    ;;
shard)
    shard="${2:?shard index}"
    total="${CI_SHARDS:-4}"
    mkdir -p reports
    python3 scripts/ci_changes.py candidates --scope full --shard "$shard" --total "$total" \
        > "reports/candidates-$shard.txt"
    echo "shard $shard/$total: $(wc -l < "reports/candidates-$shard.txt") test files"
    if [ ! -s "reports/candidates-$shard.txt" ]; then
        echo "ERROR: empty test shard $shard of $total" >&2
        exit 1
    fi
    # Large parametrize matrices run a pairwise sample unless this change set
    # can alter their outcome (task #2230); a missing decision runs them all.
    if [ "$(cat /workspace/.ci/exhaustive 2>/dev/null)" != "false" ]; then
        export UA_TEST_EXHAUSTIVE=1
    fi
    echo "exhaustive matrices: ${UA_TEST_EXHAUSTIVE:-0}"
    # Four shard containers already share one 8-vCPU, 8 GB machine; xdist
    # workers on top of them exhausted its memory (task #2230), so shards run
    # serially. ci.yaml forwards no environment into the shard containers:
    # change the default below to try workers. --dist loadfile keeps each
    # file's fixtures in one worker.
    workers="${UA_CI_PYTEST_WORKERS:-0}"
    xdist=()
    if [ "$workers" != "0" ]; then
        xdist=(-n "$workers" --dist loadfile)
    fi
    xargs -r -a "reports/candidates-$shard.txt" uv run --frozen pytest -q --tb=short \
        "${xdist[@]}" --durations=25 -o tmp_path_retention_policy=failed \
        -o junit_family=xunit1 --junitxml="reports/junit-$shard.xml"
    ;;
js)
    ensure_uv
    # Keep one Cloud Build container/status and the existing CPU/memory budget.
    # Use the same dependency decisions as hosted SDK/frontend jobs, serially.
    components="$(git diff --name-only refs/ci/base HEAD | python3 -I scripts/ci_changes.py js-components)"
    if grep -qx 'sdk=true' <<< "$components"; then
        (cd packages/protocol && npm ci && npm test && npm run build && npm run codegen:check \
            && uv run --project ../.. --group dev --locked npm run test:python)
        (cd packages/client && npm install && npm test && npm run build)
    elif grep -qx 'web=true' <<< "$components"; then
        (cd packages/protocol && npm ci && npm run build)
        (cd packages/client && npm install && npm run build)
    fi
    if grep -qx 'web=true' <<< "$components"; then
        (cd packages/web && npm ci && npm run typecheck && npm test && npm run build \
            && npx playwright install chromium && npm run test:a11y)
    fi
    ;;
native)
    cd native/UnifiedAgentDesktop
    cargo fmt --all -- --check
    cargo clippy --locked -p ua-desktop-core --all-targets -- -D warnings
    cargo test --locked -p ua-desktop-core
    ;;
*)
    echo "unknown step: $step" >&2
    exit 2
    ;;
esac
