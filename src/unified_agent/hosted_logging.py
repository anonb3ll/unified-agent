"""JSON logs for a hosted hub.

Railway (and other platforms that scrape stderr) record every stderr line as
``level=error``. The MCP server installs a Rich handler on stderr, so an INFO
line about a healthy start shows up as an error. When this process is hosted,
logs are one JSON object per line: on HTTP, INFO and WARNING use stdout and
ERROR uses stderr. Stdio keeps every log on stderr, reserving stdout for
JSON-RPC. WorkOS organization ids, user ids,
and bearer tokens are redacted before the line is written.

``install`` must run before ``MCPServer()``. That constructor calls
``logging.basicConfig``, which does nothing once the root logger already has
handlers — so the Rich handler never attaches.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping

_ORG_ID = re.compile(r"\borg_[A-Za-z0-9]+")
_USER_ID = re.compile(r"\buser_[A-Za-z0-9]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


def redact(text: str) -> str:
    """Hide WorkOS ids and bearer tokens. Other text is unchanged."""
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _ORG_ID.sub("org_[redacted]", text)
    text = _USER_ID.sub("user_[redacted]", text)
    return text


def hosted_json_logs_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True on Railway, when ``UA_HOSTED=1``, or when ``UA_LOG_FORMAT=json``."""
    env = os.environ if environ is None else environ
    if (env.get("UA_LOG_FORMAT") or "").strip().lower() == "json":
        return True
    if (env.get("UA_HOSTED") or "").strip() == "1":
        return True
    return bool((env.get("RAILWAY_ENVIRONMENT_ID") or "").strip())


def railway_level(levelno: int) -> str:
    """A level name Railway's log parser keeps (``debug``/``info``/``warn``/``error``)."""
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class BelowError(logging.Filter):
    """Let DEBUG, INFO, and WARNING through. ERROR and above go to the other handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line. The ``level`` field is the Railway name."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        payload = {
            "level": railway_level(record.levelno),
            "logger": record.name,
            "message": redact(message),
        }
        return json.dumps(payload, ensure_ascii=False)


def install(environ: Mapping[str, str] | None = None) -> bool:
    """Attach JSON handlers when hosted. Return whether they were attached.

    A no-op when JSON logs are off, so a normal developer process keeps
    whatever logging it already had. When they are on, replace the root
    handlers so a later ``basicConfig`` cannot add the stderr Rich handler.
    """
    if not hosted_json_logs_enabled(environ):
        return False
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    formatter = JsonLogFormatter()
    env = os.environ if environ is None else environ
    split_streams = env.get("UA_TRANSPORT", "stdio") == "http"
    if split_streams:
        stdout = logging.StreamHandler(sys.stdout)
        stdout.setLevel(logging.DEBUG)
        stdout.addFilter(BelowError())
        stdout.setFormatter(formatter)
        root.addHandler(stdout)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.ERROR if split_streams else logging.DEBUG)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)
    return True


def uvicorn_log_config() -> dict | None:
    """Uvicorn ``log_config`` for a hosted process, or None to keep its default.

    Passing ``log_config=None`` to uvicorn disables its logging entirely, so
    the caller must omit the argument when this returns None. The default
    uvicorn config writes INFO to stderr, which Railway then calls an error.
    """
    if not hosted_json_logs_enabled():
        return None
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "unified_agent.hosted_logging.JsonLogFormatter"}},
        "filters": {"below_error": {"()": "unified_agent.hosted_logging.BelowError"}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
                "filters": ["below_error"],
            },
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "json",
                "level": "ERROR",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["stdout", "stderr"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {
                "handlers": ["stdout", "stderr"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
