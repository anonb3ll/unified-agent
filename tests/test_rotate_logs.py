"""deploy/mac-hub/rotate-logs.sh keeps a live writer on its log (task #1960)."""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
from pathlib import Path

import pytest
from export_guard import needs_exported_paths

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "mac-hub" / "rotate-logs.sh"
# The public export drops deploy/mac-hub/ (task #2615), so the bodies that
# run the rotation script skip there and still run here.
needs_rotate_logs = needs_exported_paths(SCRIPT, repo=Path(__file__).resolve().parents[1])

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash script")


@needs_rotate_logs
def test_rotation_truncates_in_place_so_an_open_writer_keeps_logging(tmp_path):
    logdir = tmp_path / ".unified_agent"
    logdir.mkdir()
    log = logdir / "executor-agy.err"
    log.write_bytes(b"old line\n" * 500)
    inode = log.stat().st_ino

    # launchd holds service logs open with O_APPEND for the life of the job.
    with open(log, "ab", buffering=0) as writer:
        env = {**os.environ, "HOME": str(tmp_path), "UA_LOG_ROTATE_MAX": "1000"}
        env["UA_LOG_ROTATE_KEEP"] = "18"
        subprocess.run(["bash", str(SCRIPT)], env=env, check=True)
        writer.write(b"after rotation\n")

    assert log.stat().st_ino == inode
    assert log.read_bytes() == b"old line\n" * 2 + b"after rotation\n"
    with gzip.open(logdir / "executor-agy.err.old.gz") as archived:
        assert archived.read() == b"old line\n" * 500
    assert not (logdir / "executor-agy.err.tail").exists()


@needs_rotate_logs
def test_small_logs_are_left_alone(tmp_path):
    logdir = tmp_path / ".unified_agent"
    logdir.mkdir()
    log = logdir / "hub.log"
    log.write_bytes(b"small\n")
    env = {**os.environ, "HOME": str(tmp_path), "UA_LOG_ROTATE_MAX": "1000"}
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True)
    assert log.read_bytes() == b"small\n"
    assert not (logdir / "hub.log.old.gz").exists()


@needs_rotate_logs
def test_failed_archive_still_frees_the_disk(tmp_path):
    """Review: with no room for the archive, the live log must still shrink."""
    logdir = tmp_path / ".unified_agent"
    logdir.mkdir()
    log = logdir / "executor-codex.err"
    log.write_bytes(b"old line\n" * 500)
    # A directory where the archive should go makes the archive write fail.
    (logdir / "executor-codex.err.old.gz.tmp").mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "UA_LOG_ROTATE_MAX": "1000"}
    env["UA_LOG_ROTATE_KEEP"] = "18"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "could not archive" in result.stderr
    assert log.read_bytes() == b"old line\n" * 2


@needs_rotate_logs
def test_invalid_keep_is_refused_before_touching_logs(tmp_path):
    logdir = tmp_path / ".unified_agent"
    logdir.mkdir()
    log = logdir / "hub.err"
    log.write_bytes(b"x" * 2000)
    env = {**os.environ, "HOME": str(tmp_path), "UA_LOG_ROTATE_MAX": "1000"}
    env["UA_LOG_ROTATE_KEEP"] = "lots"
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert log.read_bytes() == b"x" * 2000
