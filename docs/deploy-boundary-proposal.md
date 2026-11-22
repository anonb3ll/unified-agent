# Read-Only Deployment Boundary & Release Artifact Proposal

**Status**: PROPOSAL & REHEARSAL READY (Cutover Hold Active — No Live Production Changes Made)
**Task Reference**: #394 (Parent #392)
**Author**: agent-agy
**Date**: 2026-08-13

---

## 1. Executive Summary

This proposal establishes a versioned, read-only release artifact boundary for the `unified-agent` MCP hub and `imessage-bridge` services. Currently, systemd user services execute code directly from development working directories (`~/dev/unified_agent` and `~/agent-workspace/projects/imessage-bridge`), exposing running processes to transient code edits, incomplete branches, or manual modifications.

This remediation satisfies two core directives:
1. **Source-of-Truth Reconciliation**: `~/dev/unified_agent/scripts/hermes_drift_check.sh` is established as the canonical source of truth for the drift audit script (including Tier 2 skill frontmatter monitoring), and `~/agent-workspace/projects/hermes/hermes_drift_check.sh` delegates directly to it.
2. **Read-Only Release Boundary**: Versioned release snapshot packages are generated with `MANIFEST.json` sha256 checksums, read-only permissions (`0o444`), atomic symlink cutover (`current`), and verified rollback proofs.

> [!IMPORTANT]
> **Production Cutover Status**: **HOLD / NOT AUTHORIZED**.
> Live systemd user units (`unified-agent.service`, `imessage-bridge.service`, `hermes-drift-check.service`) remain **100% unchanged**. No service restarts, token changes, or production path mutations were performed.

---

## 2. Release Artifact & Runtime Environment Architecture

```
~/.unified_agent/releases/
├── unified-agent/
│   ├── 20260813T044500_v1.0.0_fb48c18/
│   │   ├── MANIFEST.json                (sha256 manifest of all release files)
│   │   ├── pyproject.toml
│   │   ├── scripts/
│   │   │   └── hermes_drift_check.sh    (read-only 0444)
│   │   └── src/
│   │       └── unified_agent/           (read-only 0444)
│   └── current -> 20260813T044500_v1.0.0_fb48c18  (atomic symlink)
└── imessage-bridge/
    ├── 20260813T044500_v1.0.0_a1b2c3d/
    │   ├── MANIFEST.json
    │   └── src/
    └── current -> 20260813T044500_v1.0.0_a1b2c3d
```

### 2.1 Artifact Protection & Integrity
- **Excluded Files**: `.git`, `.venv`, `__pycache__`, `.pytest_cache`, `.worktrees`, `*.log`, `*.tmp`.
- **Read-Only Permissions**: All files in the release directory are set to `0o444` (read-only) and directories to `0o555` (read-exec) to guard against accidental in-place modification under standard operation.
- **Integrity & Strict Manifest Verification**: `MANIFEST.json` records relative file paths, sha256 checksums, build timestamp, creator metadata, and source git commit SHA. `verify_release()` verifies expected checksums, checks path traversal safety, and strictly rejects any unexpected file present in the release directory.

### 2.2 Runtime Environment & Virtualenv Strategy
Because `.venv` contains binary executables and host-specific virtualenv paths, it is intentionally excluded from the source snapshot package. The runtime execution model uses one of two strategies:
1. **External Shared Virtualenv (Recommended)**: Systemd units execute using an external, stable virtualenv (e.g. `%h/dev/unified_agent/.venv/bin/python3`), pointing Python module import path (`PYTHONPATH`) or script entrypoint to `%h/.unified_agent/releases/unified-agent/current/src`.
2. **Dedicated Deployment Virtualenv**: A dedicated virtualenv at `%h/.unified_agent/venv` maintained via `uv pip sync` against `uv.lock` in the active release directory.

---

## 3. Rollback & Verification Proof

The release management tool (`src/unified_agent/release.py`) provides automated checksum verification, unexpected file rejection, and atomic rollback:

```bash
# List all available releases and validity status
python3 deploy/release_rollback.py --component unified-agent --list

# Rehearse a rollback (verifies target checksums without mutating symlinks)
python3 deploy/release_rollback.py --component unified-agent --target-id <RELEASE_ID>

# Execute an authorized rollback (requires explicit --authorize flag)
python3 deploy/release_rollback.py --component unified-agent --target-id <RELEASE_ID> --authorize
```

### 3.1 Rollback Verification Lifecycle
1. Scans `~/.unified_agent/releases/<component>/` for valid releases.
2. Verifies sha256 hash of every file in the target release against its `MANIFEST.json`, and confirms no extra/unexpected files exist.
3. If valid and `--authorize` is set, atomically repoints `current` symlink via temporary link replacement (`ln -sfn ... current_tmp && mv -Tf current_tmp current`).
4. Outputs a structured JSON Rollback Proof with timestamp, previous release SHA, new release SHA, and verification status.

---

## 4. Source-of-Truth Reconciliation Summary

- **Canonical Script**: `~/dev/unified_agent/scripts/hermes_drift_check.sh`
- **Delegation Wrapper**: `~/agent-workspace/projects/hermes/hermes_drift_check.sh` delegates via `exec "$HOME"/dev/unified_agent/scripts/hermes_drift_check.sh "$@"`.
- **Test Coverage**: 19 unit tests passing in `tests/test_hermes_drift_check.py` (covering frozen files tier and monitored skill-frontmatter key tier).

---

## 5. Human Approval & Production Cutover Plan

To complete live production cutover in the future, the operator must grant explicit authorization:

1. **Build Canonical Release Artifact**:
   ```bash
   python3 deploy/build_release.py --component unified-agent --source ~/dev/unified_agent --version v1.0.0 --authorize
   ```
2. **Update Systemd Unit ExecStart Paths**:
   Update `ExecStart` in `~/.config/systemd/user/unified-agent.service` to run `%h/dev/unified_agent/.venv/bin/python3 -m unified_agent.server` with `PYTHONPATH=%h/.unified_agent/releases/unified-agent/current/src`.
3. **Daemon Reload & Restart**:
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart unified-agent imessage-bridge
   ```

*No automatic cutover will take place without the operator's explicit direction.*
