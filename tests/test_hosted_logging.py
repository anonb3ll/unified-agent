"""Hosted hubs must not write INFO to stderr.

Railway records every stderr line as an error. JSON logs put the real level
in the line, keep INFO and WARNING on stdout, and redact WorkOS ids and
bearer tokens.
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
import subprocess
import sys

import pytest

from unified_agent import hosted_logging


@pytest.fixture
def root_logs():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield root
    root.handlers.clear()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


@pytest.fixture(autouse=True)
def http_logs(monkeypatch):
    monkeypatch.setenv("UA_TRANSPORT", "http")


def _records(stream: str) -> list[dict]:
    lines = [line for line in stream.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_json_logs_are_off_unless_hosted(monkeypatch):
    monkeypatch.delenv("UA_HOSTED", raising=False)
    monkeypatch.delenv("UA_LOG_FORMAT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    assert hosted_logging.hosted_json_logs_enabled() is False
    assert hosted_logging.uvicorn_log_config() is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("UA_LOG_FORMAT", "json"),
        ("UA_HOSTED", "1"),
        ("RAILWAY_ENVIRONMENT_ID", "env-1"),
    ],
)
def test_hosted_flags_enable_json_logs(monkeypatch, key, value):
    monkeypatch.delenv("UA_HOSTED", raising=False)
    monkeypatch.delenv("UA_LOG_FORMAT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.setenv(key, value)
    assert hosted_logging.hosted_json_logs_enabled() is True
    assert hosted_logging.uvicorn_log_config() is not None


def test_install_is_a_noop_when_disabled(monkeypatch, root_logs):
    monkeypatch.delenv("UA_HOSTED", raising=False)
    monkeypatch.delenv("UA_LOG_FORMAT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    marker = logging.StreamHandler()
    root_logs.addHandler(marker)
    assert hosted_logging.install() is False
    assert marker in root_logs.handlers


def test_info_and_warning_go_to_stdout_with_railway_levels(monkeypatch, capsys, root_logs):
    monkeypatch.setenv("UA_LOG_FORMAT", "json")
    assert hosted_logging.install() is True
    log = logging.getLogger("ua.test")
    log.info("hub listening")
    log.warning("vault slow")
    out, err = capsys.readouterr()
    assert err == ""
    rows = _records(out)
    assert [row["level"] for row in rows] == ["info", "warn"]
    assert rows[0]["message"] == "hub listening"
    assert rows[1]["message"] == "vault slow"
    assert rows[0]["logger"] == "ua.test"


def test_error_and_critical_go_to_stderr_as_error(monkeypatch, capsys, root_logs):
    monkeypatch.setenv("UA_HOSTED", "1")
    hosted_logging.install()
    log = logging.getLogger("ua.test")
    log.error("store readonly")
    log.critical("store gone")
    out, err = capsys.readouterr()
    assert out == ""
    rows = _records(err)
    assert [row["level"] for row in rows] == ["error", "error"]
    assert rows[0]["message"] == "store readonly"


def test_redacts_workos_ids_and_bearer_tokens(monkeypatch, capsys, root_logs):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "env-1")
    hosted_logging.install()
    logging.getLogger("ua.test").info(
        "signed in org_01HOSTED as user_01OPERATOR with Bearer abcdefghijklmnop"
    )
    out, err = capsys.readouterr()
    assert err == ""
    text = out
    assert "org_01HOSTED" not in text
    assert "user_01OPERATOR" not in text
    assert "abcdefghijklmnop" not in text
    row = _records(out)[0]
    assert row["level"] == "info"
    assert "org_[redacted]" in row["message"]
    assert "user_[redacted]" in row["message"]
    assert "Bearer [redacted]" in row["message"]


def test_uvicorn_info_is_stdout_json_and_redacted(monkeypatch, capsys):
    monkeypatch.setenv("UA_HOSTED", "1")
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    try:
        logging.config.dictConfig(hosted_logging.uvicorn_log_config())
        logging.getLogger("uvicorn.error").info("started org_01RAIL user_01OPS")
        logging.getLogger("uvicorn.error").error("bind failed Bearer ZZZZZZZZZZZZ")
        out, err = capsys.readouterr()
    finally:
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers.clear()
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
    info = _records(out)
    error = _records(err)
    assert info[0]["level"] == "info"
    assert "org_01RAIL" not in out
    assert error[0]["level"] == "error"
    assert "ZZZZZZZZZZZZ" not in err
    assert "Bearer [redacted]" in error[0]["message"]


def test_importing_the_server_hosted_skips_the_stderr_rich_handler(tmp_path):
    """MCPServer.configure_logging must not attach RichHandler after install()."""
    env = os.environ.copy()
    env["UA_LOG_FORMAT"] = "json"
    env["UA_HOME"] = str(tmp_path / "home")
    env["UA_WORKSPACE"] = str(tmp_path / "ws")
    env["UA_STATE_SYNC"] = "0"
    script = """
import logging
import unified_agent.server
root = logging.getLogger()
names = [type(handler).__name__ for handler in root.handlers]
assert "RichHandler" not in names, names
logging.getLogger("ua.boot").info("ready org_01BOOT")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "RichHandler" not in proc.stderr
    row = json.loads(proc.stdout.strip().splitlines()[-1])
    assert row["level"] == "info"
    assert row["message"] == "ready org_[redacted]"
    assert "org_01BOOT" not in proc.stdout
    assert "org_01BOOT" not in proc.stderr


@pytest.mark.parametrize("transport", ["stdio", None])
def test_stdio_json_logs_preserve_the_protocol_stream(monkeypatch, capsys, root_logs, transport):
    monkeypatch.setenv("UA_LOG_FORMAT", "json")
    if transport is None:
        monkeypatch.delenv("UA_TRANSPORT")
    else:
        monkeypatch.setenv("UA_TRANSPORT", transport)
    hosted_logging.install()
    log = logging.getLogger("ua.protocol")
    log.info("ready")
    log.warning("configuration fallback")
    log.error("request failed")
    out, err = capsys.readouterr()
    assert out == ""
    assert [row["level"] for row in _records(err)] == ["info", "warn", "error"]


def test_real_configuration_warning_stays_off_stdio_stdout(tmp_path):
    env = os.environ.copy()
    env.update(
        UA_LOG_FORMAT="json",
        UA_TRANSPORT="stdio",
        UA_HOME=str(tmp_path / "home"),
        UA_WORKSPACE=str(tmp_path / "ws"),
        UA_STATE_SYNC="0",
        UA_EXECUTOR_TIMEOUT_S="bad-integer",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from unified_agent import server; server.config.executor_timeout_s()",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert any(row["level"] == "warn" for row in _records(proc.stderr))
