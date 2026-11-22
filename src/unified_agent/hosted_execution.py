"""Refuse to execute agent work on the hosted hub (task #2109).

Devices execute. A hosted hub (Railway, or any process started with
``UA_HOSTED=1``) is a control plane: it may record, approve, and audit, and
it must not spawn a shell, a harness CLI, or a local executor. Callers send
that work to a node worker (``ua-node``).

The canonical ``execution_locality`` policy also covers an explicit
``UA_EXECUTION_LOCALITY=devices`` on a private hub. Hosted signals
(``UA_HOSTED=1`` or ``RAILWAY_ENVIRONMENT_ID``) always require devices-only
execution, including processes started without the Docker entrypoint.

``authenticated_public`` is not this flag. A self-hosted hub can face the
public internet and still execute on that machine. Hosted means the cloud
control plane, not "auth is on".
"""

from __future__ import annotations

import os
from typing import Any

from . import execution_locality
from .tool_errors import TypedToolError

CODE = "hosted_execution_refused"


class HostedExecutionError(TypedToolError, execution_locality.ServerExecutionRefused):
    """The hosted hub will not execute this work on itself."""

    code = CODE


def is_hosted() -> bool:
    """True when this process is the hosted cloud hub."""
    if os.environ.get("UA_HOSTED", "").strip() == "1":
        return True
    return bool(os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip())


def execution_refusal_message(surface: str) -> str:
    return f"{execution_locality.refusal_message(surface)} Use a node worker (ua-node)."


def refuse_hosted_execution(surface: str) -> None:
    """Raise when this process must not spawn local work. No-op otherwise."""
    if execution_locality.devices_only():
        raise HostedExecutionError(execution_refusal_message(surface), surface=surface)


def refusal_result(surface: str) -> dict[str, Any] | None:
    """Spawn-result shape for callers that report failure instead of raising."""
    if not execution_locality.devices_only():
        return None
    return {
        "ok": False,
        "spawned": False,
        "pid": None,
        "code": CODE,
        "error": execution_refusal_message(surface),
    }
