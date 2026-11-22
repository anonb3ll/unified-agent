"""``_shorten_tmpdir_for_unix_sockets`` keeps pytest's temp roots short enough
for ``AF_UNIX`` socket paths on every non-Windows platform (tasks #2040, #2293)."""

import tempfile

import conftest
import pytest

LONG = "/var/folders/" + "x" * 40
SHORT = "/tmp/x"


@pytest.fixture(params=["darwin", "linux"])
def unix(request, monkeypatch):
    monkeypatch.setattr(conftest.sys, "platform", request.param)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setenv("TMPDIR", SHORT)
    return monkeypatch


def test_long_debug_temproot_is_shortened(unix):
    unix.setenv("PYTEST_DEBUG_TEMPROOT", LONG)
    conftest._shorten_tmpdir_for_unix_sockets()
    assert conftest.os.environ["PYTEST_DEBUG_TEMPROOT"] == "/tmp/ua-pytest"


def test_short_debug_temproot_is_kept(unix):
    unix.setenv("PYTEST_DEBUG_TEMPROOT", SHORT)
    conftest._shorten_tmpdir_for_unix_sockets()
    assert conftest.os.environ["PYTEST_DEBUG_TEMPROOT"] == SHORT
    assert conftest.os.environ["TMPDIR"] == SHORT


def test_unset_debug_temproot_stays_unset(unix):
    unix.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    unix.setenv("TMPDIR", LONG)
    conftest._shorten_tmpdir_for_unix_sockets()
    assert "PYTEST_DEBUG_TEMPROOT" not in conftest.os.environ
    assert conftest.os.environ["TMPDIR"] == "/tmp/ua-pytest"


def test_windows_untouched(monkeypatch):
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", LONG)
    monkeypatch.setenv("TMPDIR", LONG)
    conftest._shorten_tmpdir_for_unix_sockets()
    assert conftest.os.environ["PYTEST_DEBUG_TEMPROOT"] == LONG
    assert conftest.os.environ["TMPDIR"] == LONG
