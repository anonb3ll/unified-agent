"""Stdlib ACP client: JSON-RPC 2.0 over the agent's stdio (task #1328, I2).

ua-node is the ACP *client*; the harness binary is the *agent* subprocess.
Framing is newline-delimited JSON — one message per line on stdin/stdout,
stderr is the agent's own log. No SDK dependency: the wire is small enough
to own (initialize, session/new, session/prompt, session/cancel, inbound
session/update notifications and session/request_permission requests), plus
the ``_session/steering`` extension request, sent only to an agent that
advertised it at initialize (task #1740), and ``session/resume`` /
``session/load``, sent only to an agent that advertised the matching
capability (task #1741).

Design notes:

- ACP v1 is the wire today. ``initialize`` sends ``protocolVersion: 1`` and
  the agent echoes what it speaks; the negotiated value is recorded on the
  client (``.protocol_version``) so callers can refuse what they cannot
  drive. v2 is gated off — ``UA_ACP_PROTOCOL_VERSION=2`` raises instead of
  silently speaking a dialect this client does not implement (the v2 draft
  moves turn completion to ``state_update`` and drops the client fs/terminal
  surface).
- Inbound agent→client requests we do not implement (fs/*, terminal/*,
  mcp/*, elicitation/* — none of which we advertise) are answered with
  JSON-RPC ``method not found`` rather than left to hang the agent.
- ``session/request_permission`` is routed to a caller-supplied callback;
  the default answer is ``{"outcome": {"outcome": "cancelled"}}`` — fail
  closed, never auto-approve.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import session_settings

logger = logging.getLogger("unified_agent.acp")

PROTOCOL_VERSION = 1
# Per-update payload cap mirrored on the node side: an oversized
# session/update is replaced by a stub, never allowed to grow a buffer.
MAX_UPDATE_BYTES = 32_768
# After cancel/close the agent gets this long to exit before SIGKILL.
KILL_GRACE_S = 2.0
# The agent leads its own session and process group, so terminate() can stop
# the tool subprocesses it spawned along with it (task #1984). POSIX only.
_OWN_PROCESS_GROUP = hasattr(os, "killpg")


# Process states that are no longer running: zombie, dead.
_DEAD_STATES = frozenset("ZX")
# Linux process table; absent on macOS, where ``ps`` lists group members.
_PROC = Path("/proc")


def _proc_stat(path: Path) -> tuple[str, int] | None:
    """``(state, pgrp)`` from a Linux ``/proc/<pid>/stat``, or None if gone.

    Read as bytes: the command name is process-controlled and need not be
    UTF-8 (``prctl(PR_SET_NAME)``), and it may contain spaces or parens.
    Only disappearance reads as gone. Any other read error (EACCES, say)
    propagates: the process may be a surviving group member (#1149).
    """
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = raw.rsplit(b")", 1)[-1].split()
    try:
        return fields[0].decode("ascii", "replace"), int(fields[2])
    except (IndexError, ValueError):
        return None


class AcpError(RuntimeError):
    """Transport or protocol failure talking to an ACP agent."""


def _offer(waiter: queue.Queue, message: dict[str, Any]) -> None:
    """Hand a reply to its waiter without ever blocking the reader thread."""
    try:
        waiter.put_nowait(message)
    except queue.Full:
        pass


def _env_protocol_version() -> int:
    raw = os.environ.get("UA_ACP_PROTOCOL_VERSION", "").strip()
    if not raw:
        return PROTOCOL_VERSION
    try:
        version = int(raw)
    except ValueError:
        raise AcpError(f"UA_ACP_PROTOCOL_VERSION must be an integer, got {raw!r}") from None
    if version != PROTOCOL_VERSION:
        raise AcpError(
            f"ACP protocol v{version} is gated off: this client speaks v{PROTOCOL_VERSION} "
            "only; v2 (draft) needs the state_update turn lifecycle, which is not implemented"
        )
    return version


class AcpClient:
    """One ACP agent subprocess and its JSON-RPC channel.

    ``on_update(session_id, update)`` receives every ``session/update``
    notification (the normalized-but-unflattened update dict).
    ``on_permission(request) -> dict`` must return the JSON-RPC *result* for
    a ``session/request_permission`` call — e.g.
    ``{"outcome": {"outcome": "selected", "optionId": "..."}}``; returning
    None or raising answers ``cancelled``.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        session_dir: str | None = None,
        on_update: Callable[[str, dict[str, Any]], None] | None = None,
        on_permission: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        stderr_name: str = "acp-agent.stderr.log",
    ):
        if not argv:
            raise AcpError("acp argv must not be empty")
        from . import execution_locality

        if execution_locality.devices_only():
            raise AcpError(execution_locality.refusal_message("ACP agent process"))
        self.argv = list(argv)
        self._on_update = on_update
        self._on_permission = on_permission
        self._want_version = _env_protocol_version()
        self.protocol_version: int | None = None
        self.agent_capabilities: dict[str, Any] = {}
        self.agent_info: dict[str, Any] = {}
        self.init_meta: dict[str, Any] = {}
        self._settings_lock = threading.RLock()
        self.session_setup: dict[str, Any] = {}
        self.active_session_id: str | None = None
        self._setting_updates = {"mode": 0, "model": 0}
        self._id = 0
        self._write_lock = threading.RLock()
        self._permissions_blocked = False
        self._pending: dict[Any, queue.Queue] = {}
        self._setup_requests: dict[Any, dict[str, Any]] = {}
        self._closed = threading.Event()
        # terminate() runs once; a concurrent caller waits for that run, so
        # every caller returns only after the agent's group is stopped.
        self._terminate_lock = threading.RLock()
        self._terminated = False
        # Exact values (a session credential, task #1744) scrubbed from any
        # agent output this client logs.
        self._redact: list[str] = []
        log_path = Path(session_dir or ".") / stderr_name
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = open(log_path, "ab", buffering=0)
        except OSError:
            self._stderr = subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                cwd=cwd or None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=_OWN_PROCESS_GROUP,
            )
        except OSError as exc:
            # Constructor raised before the caller got an instance — close the
            # stderr log ourselves so a broken harness cannot leak descriptors.
            if hasattr(self._stderr, "close"):
                try:
                    self._stderr.close()
                except Exception:  # noqa: BLE001
                    pass
            raise AcpError(f"could not spawn ACP agent {self.argv[0]!r}: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name=f"acp-reader-{self._proc.pid}", daemon=True
        )
        self._reader.start()

    # -- transport -----------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        if self._closed.is_set() or self._proc.stdin is None or self._proc.stdin.closed:
            raise AcpError("acp channel is closed")
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(f"acp write failed: {exc}") from exc

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.warning("acp: dropping non-JSON line: %.200s", self.scrub(line))
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            for waiter in list(self._pending.values()):
                # Never block the reader: a waiter whose reply already landed
                # (and was not yet taken) is full, and needs no exit wake.
                _offer(waiter, {"error": {"code": -32000, "message": "agent exited"}})
            self._pending.clear()
            with self._settings_lock:
                self._setup_requests.clear()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            waiter = self._pending.pop(msg["id"], None)
            with self._settings_lock:
                setup = self._setup_requests.pop(msg["id"], None)
                if waiter is not None and setup is not None and "result" in msg:
                    result = msg["result"]
                    sid = (
                        result.get("sessionId")
                        if setup["method"] == "session/new" and isinstance(result, dict)
                        else setup["session_id"]
                    )
                    if (
                        not isinstance(result, dict)
                        or not isinstance(sid, str)
                        or not sid
                        or len(sid) > 200
                        or setup["overflow"]
                    ):
                        msg = {"error": {"code": -32000, "message": "invalid session setup"}}
                    else:
                        # The reader installs setup before releasing the caller.
                        # Subsequent notifications are not dropped during new
                        # or overwritten during resume/load. Before a new reply
                        # the session ID is unknown: those updates are ignored.
                        self.active_session_id = sid
                        self.session_setup = dict(result)
                        for update in setup["updates"]:
                            self._apply_settings_update(update)
            if waiter is not None:
                _offer(waiter, msg)
            return
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if "id" in msg:
            self._handle_inbound_request(msg)
            return
        if method == "session/update":
            params = msg.get("params") or {}
            with self._settings_lock:
                if (
                    params.get("sessionId") != self.active_session_id
                    or self.active_session_id is None
                ):
                    return
                update = params.get("update") or {}
                variant = update.get("sessionUpdate") if isinstance(update, dict) else None
                if variant in (
                    "current_mode_update",
                    "current_model_update",
                    "config_option_update",
                ):
                    for setup in self._setup_requests.values():
                        if setup["session_id"] == self.active_session_id:
                            if (
                                len(setup["updates"]) < 128
                                and len(json.dumps(update).encode("utf-8")) <= MAX_UPDATE_BYTES
                            ):
                                setup["updates"].append(update)
                            else:
                                setup["overflow"] = True
                    self._apply_settings_update(update)
            if self._on_update is not None:
                try:
                    self._on_update(str(params.get("sessionId") or ""), params.get("update") or {})
                except Exception:  # noqa: BLE001 - a bad callback must not kill the reader
                    logger.exception("acp: on_update callback raised")
        elif method == "$/cancel_request":
            # Peer abandoned an outbound request; nothing to do — the response
            # would be dropped anyway.
            pass
        else:
            logger.debug("acp: ignoring notification %r", method)

    def _apply_settings_update(self, update: dict[str, Any]) -> None:
        """Mutate observed settings with the state lock held; never call user code."""
        variant = update.get("sessionUpdate")
        if variant in ("current_mode_update", "current_model_update"):
            kind = "mode" if variant == "current_mode_update" else "model"
            self._setting_updates[kind] += 1
            field = kind + "s"
            current_key = f"current{kind.title()}Id"
            existing = self.session_setup.get(field)
            native = dict(existing) if isinstance(existing, dict) else {}
            native[current_key] = update.get(current_key)
            options = self.session_setup.get("configOptions")
            extra = {}
            if isinstance(options, list):
                extra["configOptions"] = [
                    {**o, "currentValue": update.get(current_key)}
                    if isinstance(o, dict) and o.get("category") == kind
                    else o
                    for o in options
                ]
            self.session_setup = {**self.session_setup, field: native, **extra}
        elif variant == "config_option_update":
            if isinstance(update.get("configOptions"), list):
                self._setting_updates["mode"] += 1
                self._setting_updates["model"] += 1
                self.session_setup = {
                    **self.session_setup,
                    "configOptions": update["configOptions"],
                }

    def block_permissions(self) -> None:
        """Fence pending permission replies at the same lock as wire writes."""
        with self._write_lock:
            self._permissions_blocked = True

    def add_redaction(self, value: str) -> None:
        """Never log ``value`` verbatim (a session credential the agent might echo)."""
        if value and value not in self._redact:
            self._redact.append(value)

    def scrub(self, text: str) -> str:
        for value in self._redact:
            text = text.replace(value, "[REDACTED_SECRET]")
        return text

    # -- inbound agent->client requests --------------------------------------

    def _handle_inbound_request(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            # Permission callbacks may poll the hub for minutes. Run them off
            # the sole reader thread so prompt replies and cancel still flow.
            payload = params if isinstance(params, dict) else {}

            def _answer_permission() -> None:
                result: dict[str, Any] | None = None
                if (
                    self._on_permission is not None
                    and self.active_session_id is not None
                    and payload.get("sessionId") == self.active_session_id
                ):
                    try:
                        result = self._on_permission(payload)
                    except Exception:  # noqa: BLE001 - fail closed
                        logger.exception("acp: on_permission callback raised")
                        result = None
                if not isinstance(result, dict):
                    result = {"outcome": {"outcome": "cancelled"}}
                try:
                    with self._write_lock:
                        if self._permissions_blocked:
                            result = {"outcome": {"outcome": "cancelled"}}
                        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
                except AcpError:
                    logger.warning("acp: could not answer inbound %r (id=%r)", method, req_id)

            threading.Thread(
                target=_answer_permission, name=f"acp-perm-{req_id}", daemon=True
            ).start()
            return
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"client does not implement {method!r}"},
        }
        try:
            self._send(response)
        except AcpError:
            logger.warning("acp: could not answer inbound %r (id=%r)", method, req_id)

    # -- RPC ------------------------------------------------------------------

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 60.0
    ) -> dict[str, Any]:
        """One request/response round-trip; raises AcpError on wire or RPC error."""
        with self._write_lock:
            self._id += 1
            req_id = self._id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._pending[req_id] = waiter
        if method in ("session/new", "session/resume", "session/load"):
            with self._settings_lock:
                self._setup_requests[req_id] = {
                    "method": method,
                    "session_id": (params or {}).get("sessionId"),
                    "updates": [],
                    "overflow": False,
                }
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        except Exception:
            self._pending.pop(req_id, None)
            with self._settings_lock:
                self._setup_requests.pop(req_id, None)
            raise
        try:
            reply = waiter.get(timeout=timeout_s)
        except queue.Empty as exc:
            self._pending.pop(req_id, None)
            with self._settings_lock:
                self._setup_requests.pop(req_id, None)
            raise AcpError(f"acp {method} timed out after {timeout_s}s") from exc
        if "error" in reply:
            err = reply["error"] or {}
            raise AcpError(f"acp {method} failed: {err.get('code')} {err.get('message')}")
        return reply.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- ACP surface ------------------------------------------------------------

    def initialize(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self._want_version,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "ua-node", "title": "unified-agent ua-node"},
            },
            timeout_s=timeout_s,
        )
        self.protocol_version = result.get("protocolVersion")
        self.agent_capabilities = result.get("agentCapabilities") or {}
        self.agent_info = result.get("agentInfo") or {}
        meta = result.get("_meta")
        self.init_meta = meta if isinstance(meta, dict) else {}
        return result

    @property
    def steering_supported(self) -> bool:
        """True only when initialize advertised ``_meta.steering.supported: true``.

        That is the ACP steering extension (``_session/steering``): a message
        injected into the running turn. Nothing else is taken as proof that an
        agent accepts mid-turn input.
        """
        steering = self.init_meta.get("steering")
        return isinstance(steering, dict) and steering.get("supported") is True

    @property
    def mcp_http_supported(self) -> bool:
        """True only when initialize advertised ``mcpCapabilities.http: true``.

        That is the ACP way for an agent to say it accepts an HTTP ``McpServer``
        in ``session/new``, ``session/resume`` and ``session/load`` (task #1744).
        """
        caps = self.agent_capabilities if isinstance(self.agent_capabilities, dict) else {}
        mcp = caps.get("mcpCapabilities")
        return isinstance(mcp, dict) and mcp.get("http") is True

    @property
    def resume_mode(self) -> str | None:
        """How this agent can reopen an existing session, from initialize only.

        ``"resume"``: ``agentCapabilities.sessionCapabilities.resume`` is
        present, so ``session/resume`` reconnects without replaying history
        (stable in ACP v1 since 2026-04). ``"load"``: ``agentCapabilities.
        loadSession`` is true, so ``session/load`` reopens it and replays the
        conversation as ``session/update`` notifications first. Resume wins
        when both are advertised: the hub already holds the transcript, so a
        replay is only noise. None means neither: the session cannot be
        reopened and a new run starts a fresh one.
        """
        caps = self.agent_capabilities if isinstance(self.agent_capabilities, dict) else {}
        session_caps = caps.get("sessionCapabilities")
        if isinstance(session_caps, dict) and isinstance(session_caps.get("resume"), dict):
            return "resume"
        if caps.get("loadSession") is True:
            return "load"
        return None

    def resume_session(
        self,
        session_id: str,
        cwd: str,
        *,
        timeout_s: float = 60.0,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        """``session/resume``: reconnect to ``session_id`` without a replay."""
        with self._settings_lock:
            self.active_session_id = session_id
        self.request(
            "session/resume",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout_s=timeout_s,
        )

    def load_session(
        self,
        session_id: str,
        cwd: str,
        *,
        timeout_s: float = 120.0,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        """``session/load``: the agent replays history as updates, then answers."""
        with self._settings_lock:
            self.active_session_id = session_id
        self.request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout_s=timeout_s,
        )

    def new_session(
        self,
        cwd: str,
        *,
        timeout_s: float = 60.0,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        """``session/new``. ``mcp_servers`` is passed verbatim (task #1744);
        the default is the empty list every session got before."""
        result = self.request(
            "session/new",
            {"cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout_s=timeout_s,
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise AcpError("session/new returned no valid sessionId")
        return session_id

    def session_settings(self, harness: str) -> dict[str, Any]:
        with self._settings_lock:
            return session_settings.advertised(self.session_setup, harness)

    def set_session_setting(
        self, session_id: str, kind: str, value: str, *, harness: str, timeout_s: float = 30.0
    ) -> None:
        """Set only an advertised id, using its advertised v1 setting surface."""
        with self._settings_lock:
            if session_id != self.active_session_id:
                raise AcpError("setting_session_mismatch: not the active session")
            settings = self.session_settings(harness)
            revisions = dict(self._setting_updates)
        if kind not in ("mode", "model") or value not in {
            item["id"] for item in settings.get(f"available_{kind}s", [])
        }:
            raise AcpError(f"{kind}_unsupported: requested id was not advertised")
        path = settings[f"{kind}_path"]
        if path == "session/set_config_option":
            result = self.request(
                path,
                {
                    "sessionId": session_id,
                    "configId": settings[f"{kind}_config_id"],
                    "value": value,
                },
                timeout_s=timeout_s,
            )
            if not isinstance(result.get("configOptions"), list):
                raise AcpError(f"{kind}_unconfirmed: configOptions response missing")
            with self._settings_lock:
                candidate = {**self.session_setup, "configOptions": result["configOptions"]}
                incoming = session_settings.advertised(candidate, harness)
                current = self.session_settings(harness)
                # Config responses replace the entire option list, so a model
                # response must not hide a newer conflicting permission update.
                for changed in ("mode", "model"):
                    if (
                        self._setting_updates[changed] != revisions[changed]
                        and current[f"current_{changed}_id"] != incoming[f"current_{changed}_id"]
                    ):
                        raise AcpError(
                            f"{changed}_unconfirmed: newer update conflicts with setting response"
                        )
                if incoming[f"current_{kind}_id"] != value:
                    raise AcpError(
                        f"{kind}_unconfirmed: agent returned a different effective setting"
                    )
                self.session_setup = candidate
        else:
            self.request(path, {"sessionId": session_id, f"{kind}Id": value}, timeout_s=timeout_s)
            with self._settings_lock:
                if (
                    self._setting_updates[kind] != revisions[kind]
                    and self.session_settings(harness)[f"current_{kind}_id"] != value
                ):
                    raise AcpError(
                        f"{kind}_unconfirmed: agent reported a different effective setting"
                    )
                field = "modes" if kind == "mode" else "models"
                existing = self.session_setup.get(field)
                native = dict(existing) if isinstance(existing, dict) else {}
                native[f"current{kind.title()}Id"] = value
                self.session_setup = {**self.session_setup, field: native}

    def prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout_s: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """v1: the response ends the turn (stopReason). Poll ``cancel_check``
        while waiting so a hub cancellation still reaches a blocked turn.
        ``on_sent`` runs once the request is written to the agent, and never
        when the write fails, so a caller can tell sent from unsent."""
        req_id, waiter = self._start(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )
        if on_sent is not None:
            on_sent()
        return self._await(
            req_id,
            waiter=waiter,
            timeout_s=timeout_s,
            cancel_check=cancel_check,
            session_id=session_id,
        )

    def steer(self, session_id: str, text: str, *, timeout_s: float = 15.0) -> str:
        """Send ``_session/steering`` and return the agent's ``outcome``.

        ``injected`` means the message joined the running turn. The request
        opts into ``idleBehavior: promptRequired``, so an agent whose turn has
        already settled answers ``promptRequired`` and leaves the message for a
        normal ``session/prompt``. Raises AcpError on any wire or RPC error;
        the caller falls back to cancel and re-prompt.
        """
        result = self.request(
            "_session/steering",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
                "_meta": {"steering": {"idleBehavior": "promptRequired"}},
            },
            timeout_s=timeout_s,
        )
        return str(result.get("outcome") or "")

    def cancel(self, session_id: str) -> None:
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except AcpError:
            pass

    # -- request plumbing -------------------------------------------------------

    def _start_request(self, method: str, params: dict[str, Any]) -> Any:
        return self._start(method, params)[0]

    def _start(self, method: str, params: dict[str, Any]) -> tuple[Any, queue.Queue]:
        """Write one request; return its id and the waiter its reply lands in.

        The caller keeps the waiter: a fast agent can answer, and ``_dispatch``
        pop the id, before the caller looks it up again.
        """
        with self._write_lock:
            self._id += 1
            req_id = self._id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        waiter._method = method  # noqa: SLF001 - annotate for error context
        # Registered exactly once, before the write: a fast reply pops it in
        # ``_dispatch``, and registering again afterwards would leave a full
        # waiter behind for the exit cleanup to trip over (task #1741 review).
        self._pending[req_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        except Exception:
            self._pending.pop(req_id, None)
            raise
        return req_id, waiter

    def _await(
        self,
        req_id: Any,
        *,
        timeout_s: float | None,
        cancel_check: Callable[[], bool] | None,
        session_id: str,
        waiter: queue.Queue | None = None,
    ) -> dict[str, Any]:
        # Hold a local reference for the whole wait. `_dispatch` pops the id
        # from `_pending` when the reply arrives; re-looking it up each slice
        # races fast agents and wrongly reports a lost waiter.
        if waiter is None:
            waiter = self._pending.get(req_id)
        if waiter is None:
            raise AcpError("acp session/prompt lost its waiter")
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        cancelled = False
        while True:
            slice_s = 0.5
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    raise AcpError(f"acp session/prompt timed out after {timeout_s}s")
                slice_s = min(slice_s, remaining)
            try:
                reply = waiter.get(timeout=slice_s)
            except queue.Empty:
                if self._closed.is_set() and self._pending.get(req_id) is None:
                    # Agent exited and `_read_loop` already delivered the wake.
                    # Another Empty means we missed it — fail closed.
                    raise AcpError("acp session/prompt lost its waiter (agent exited)") from None
                if cancel_check is not None and cancel_check() and not cancelled:
                    cancelled = True
                    self.cancel(session_id)
                continue
            self._pending.pop(req_id, None)
            if "error" in reply:
                err = reply["error"] or {}
                raise AcpError(f"acp session/prompt failed: {err.get('code')} {err.get('message')}")
            return reply.get("result") or {}

    # -- lifecycle ----------------------------------------------------------------

    def terminate(self) -> bool:
        """Stop the agent and the processes it spawned in its process group.

        The agent leads its own process group, which its tool subprocesses
        inherit. SIGTERM goes to the whole group; the group then gets up to
        KILL_GRACE_S to exit before SIGKILL stops whatever is left. Signalling
        only the agent pid let those children keep writing to the worktree
        after the session ended (task #1984). The group is swept even when the
        agent already exited, since its children outlive it. A descendant that
        leaves the group (``setsid()`` or ``setpgid()``) is outside the sweep;
        process groups cannot follow it. A final group SIGKILL is always sent
        before the leader is reaped. After SIGKILL it waits, bounded, until no
        live member is left.

        Returns True once the group is confirmed stopped, and only then latches:
        later calls return True at once, and a concurrent one waits for the
        first. False means unconfirmed (a group signal failed, members still
        ran after the SIGKILL wait, or the leader was reaped outside the
        client so the group can no longer be addressed safely). An unconfirmed
        run keeps the leader unreaped, so the pgid stays anchored and a retry
        signals the group again. Each call is bounded by about two grace
        windows; it never hangs.
        """
        with self._terminate_lock:
            if self._terminated:
                return True
            if not _OWN_PROCESS_GROUP:
                self._terminate_agent_only()
                self._terminated = self._proc.returncode is not None
                return self._terminated
            if self._proc.returncode is not None:
                # The leader was already reaped. Its pid, and so the pgid
                # number, may since have been recycled, so signalling the group
                # could hit an unrelated one, and its members cannot be
                # confirmed gone. exited() never reaps, which keeps this to a
                # leader reaped outside the client.
                logger.warning(
                    "ACP agent pid %s was reaped outside the client; its process "
                    "group cannot be swept or confirmed stopped",
                    self._proc.pid,
                )
                return False
            # From here the leader is running or an unreaped zombie, so its pid
            # is still allocated and the pgid cannot name anyone else's group.
            pgid = self._proc.pid
            deadline = time.monotonic() + KILL_GRACE_S
            self._signal_group(pgid, signal.SIGTERM)
            # Keep the leader (including its zombie) allocated through EVERY
            # group signal. Reaping after SIGTERM could recycle the pgid before
            # the probe/SIGKILL below. _group_alive() counts live members only,
            # so the zombie anchor alone does not hold the grace window open.
            while self._group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.05)
            # Always end with a group SIGKILL before reaping. A negative member
            # scan only shortens the grace wait: a /proc snapshot can miss a
            # descendant forked mid-scan, so it never decides whether to kill.
            # The unreaped leader still anchors the pgid, and SIGKILL to a group
            # whose members have all exited is harmless.
            killed = self._signal_group(pgid, signal.SIGKILL)
            # Wait for the members themselves, not only the leader, so no
            # descendant is running when this reports the group stopped. The
            # leader stays unreaped meanwhile, keeping the pgid allocated.
            kill_deadline = time.monotonic() + KILL_GRACE_S
            while self._group_alive(pgid) and time.monotonic() < kill_deadline:
                time.sleep(0.02)
            if not killed or self._group_alive(pgid):
                logger.warning(
                    "ACP agent pid %s: process group not confirmed stopped "
                    "(SIGKILL %s, %.1fs wait); leaving it retryable",
                    pgid,
                    "delivered" if killed else "failed",
                    KILL_GRACE_S,
                )
                return False
            # Confirmed: only the leader's zombie (if anything) is left. Reap it.
            try:
                self._proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                return False
            self._terminated = True
            return True

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> bool:
        """Send ``sig`` to the group; False when delivery failed.

        A group that is already gone counts as delivered: ESRCH, or EPERM on
        Darwin, which reports a group with no live member that way.
        """
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return sys.platform == "darwin"
        except OSError:
            return False
        return True

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        """Is any process in ``pgid`` still alive (running, not a zombie)?

        Mirrors ``dispatch.supervise._pgid_alive`` (importing it would pull in
        the whole dispatch package): Darwin raises EPERM, not ESRCH, from
        ``killpg(pgid, 0)`` once no live process is left in the group.

        ``killpg(pgid, 0)`` also succeeds while the only member is the
        leader's unreaped zombie, which terminate() keeps as the pgid anchor.
        So a group the probe reports is checked for a live member, and is
        presumed alive when its members cannot be listed. Never raises: an
        unexpected error listing members also reads as alive.
        """
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return sys.platform != "darwin"
        except OSError:
            return False
        try:
            # An unreadable member (not a vanished one) raises here: unknown, so alive.
            live = AcpClient._group_has_live_member(pgid)
        except Exception:  # noqa: BLE001 - fail toward signalling, never abort the sweep
            logger.debug("process group %s: member listing failed", pgid, exc_info=True)
            live = None
        return True if live is None else live

    @staticmethod
    def _group_has_live_member(pgid: int) -> bool | None:
        """Whether a non-zombie process is in ``pgid``; None when unknowable."""
        proc = _PROC
        if (proc / "self" / "stat").exists():
            leader = _proc_stat(proc / str(pgid) / "stat")
            if leader is not None and leader[1] == pgid and leader[0] not in _DEAD_STATES:
                return True  # the common case: no scan while the leader runs
            try:
                entries = [e for e in os.listdir(proc) if e.isdigit()]
            except OSError:
                return None
            for entry in entries:
                try:
                    stat = _proc_stat(proc / entry / "stat")
                except PermissionError:
                    # hidepid/ProtectProc hides other processes' stat: ask the
                    # kernel for the group, so an unrelated one cannot pin it.
                    try:
                        if os.getpgid(int(entry)) == pgid:
                            return True
                    except ProcessLookupError:
                        pass
                    continue
                if stat is not None and stat[1] == pgid and stat[0] not in _DEAD_STATES:
                    return True
            return False
        try:
            listing = subprocess.run(
                ["/bin/ps", "-A", "-o", "pgid=,stat="],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if listing.returncode != 0:
            return None
        for line in listing.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == str(pgid) and fields[1][0] not in _DEAD_STATES:
                return True
        return False

    def _terminate_agent_only(self) -> None:
        """Pre-#1984 fallback where process groups do not exist."""
        if self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
        except OSError:
            return
        try:
            self._proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=KILL_GRACE_S)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self) -> None:
        self.terminate()
        try:
            self._reader.join(timeout=KILL_GRACE_S)
        except RuntimeError:
            pass
        try:
            if hasattr(self._stderr, "close"):
                self._stderr.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def pid(self) -> int:
        return self._proc.pid

    def exited(self) -> int | None:
        """The agent's exit code, or None while it runs.

        Until terminate() has run this peeks without reaping (``WNOWAIT``):
        the exited agent stays a zombie, so its pid, which is also the
        process-group id terminate() signals, cannot be recycled (task #1984).
        On POSIX runtimes without waitid (macOS Python 3.11/3.12), ps observes
        zombie state without waiting on the child. Once it exits, sweep the
        group before reaping to obtain the actual exit code. Pipe EOF is not
        sufficient: a tool child can inherit stdout and hold it open.
        """
        with self._terminate_lock:
            if self._proc.returncode is not None or self._terminated or not _OWN_PROCESS_GROUP:
                return self._proc.poll()
            waitid = getattr(os, "waitid", None)
            if waitid is None or not hasattr(os, "WNOWAIT"):
                return self._exited_without_waitid()
            try:
                info = waitid(os.P_PID, self._proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return self._proc.poll()
            if info is None or info.si_pid == 0:
                return None
            if info.si_code == os.CLD_EXITED:
                return info.si_status
            return -info.si_status

    def _exited_without_waitid(self) -> int | None:
        """Observe the owned leader without reaping; caller holds the lifecycle lock."""
        try:
            state = subprocess.run(
                ["/bin/ps", "-o", "stat=", "-p", str(self._proc.pid)],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcpError(
                "cannot observe ACP agent exit without waitid; close the session"
            ) from exc
        status = state.stdout.strip()
        if state.returncode not in (0, 1) or state.stderr.strip():
            raise AcpError("cannot observe ACP agent exit without waitid; close the session")
        if not status:
            # Reaped externally: collect Popen's status, but never signal a
            # group that no longer has our leader as its allocation anchor.
            return self._proc.poll()
        if status.startswith("Z"):
            if not self.terminate():
                raise AcpError(
                    "ACP agent exited but its process group was not confirmed stopped; "
                    "close the session"
                )
            return self._proc.returncode
        return None

    def __enter__(self) -> AcpClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
