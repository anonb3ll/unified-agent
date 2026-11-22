"""Conditional filesystem writes retain exact reviewed base and legacy behavior."""

import hashlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools import approvals, files


def test_absent_precondition_refuses_local_creation(isolated_env):
    destination = isolated_env / "file.txt"
    destination.write_text("Local work")
    with pytest.raises(ToolError, match="expected_content_hash"):
        files.fs_write("file.txt", "Reviewed content", expected_content_hash="")
    assert destination.read_text() == "Local work"


def test_writer_rechecks_base_after_approval_hook(isolated_env, monkeypatch):
    destination = isolated_env / "file.txt"
    destination.write_text("Version A")
    base = hashlib.sha256(destination.read_bytes()).hexdigest()

    def changed(*args, **kwargs):
        destination.write_text("Version B")
        return {}

    monkeypatch.setattr(approvals, "check_approval_gate", changed)
    with pytest.raises(ToolError, match="expected_content_hash"):
        files.fs_write("file.txt", "Reviewed content", expected_content_hash=base)
    assert destination.read_text() == "Version B"


def test_legacy_write_retains_canonical_approval_payload(isolated_env, monkeypatch):
    observed = []
    monkeypatch.setattr(
        approvals,
        "check_approval_gate",
        lambda tool, payload, **kwargs: observed.append(payload) or {},
    )
    files.fs_write("file.txt", "Legacy content")
    assert observed == [
        {
            "path": "file.txt",
            "append": False,
            "content_sha256": hashlib.sha256(b"Legacy content").hexdigest(),
            "bytes": len(b"Legacy content"),
        }
    ]


@pytest.mark.parametrize("value", [True, 1, "bad"])
def test_conditional_write_refuses_invalid_precondition(isolated_env, value):
    with pytest.raises(ToolError, match="expected_content_hash"):
        files.fs_write("file.txt", "Reviewed content", expected_content_hash=value)
    assert not (isolated_env / "file.txt").exists()


def test_writer_refuses_destination_change_during_approval(isolated_env, monkeypatch):
    destination = isolated_env / "file.txt"
    destination.write_text("Version A")
    base = hashlib.sha256(destination.read_bytes()).hexdigest()

    def changed(*args, **kwargs):
        destination.unlink()
        destination.symlink_to(isolated_env / "other.txt")
        return {}

    monkeypatch.setattr(approvals, "check_approval_gate", changed)
    with pytest.raises(
        ToolError, match="conditional writes refuse symlink destinations|write destination changed"
    ):
        files.fs_write("file.txt", "Reviewed content", expected_content_hash=base)
