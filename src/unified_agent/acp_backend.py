"""AcpBackend: run a claimed node item through an ACP harness (task #1328).

The executor contract stays identical to the CLI backends — the node does
not care whether a run went through argv scraping or ACP. This backend owns
spawn→initialize→session/new→prompt→(updates, permission calls)→stopReason
and normalizes ``session/update`` notifications into bounded event dicts
forwarded to the hub (provenance keeps the raw variant; we do not flatten
ACP into a bespoke semantic model).

Turn end is a *derived* signal, not "the prompt call returned" — v1 ends a
turn on the ``session/prompt`` response's ``stopReason``; the v2 draft moves
that to an idle ``state_update``. Keeping the distinction out of the event
contract now is what keeps the normalized stream stable when v2 lands.

Turn lifecycle (task #1740). One session may now run several turns: the
initial prompt, then operator follow-ups. Every event carries ``turn_id``
(None outside a turn), and the backend adds normalized lifecycle kinds next
to the raw ``sessionUpdate`` kinds, which keep their own names:

``turn_start``     ``{turn_id, index, source: initial|queue|steer, followup_ids}``
``turn_end``       ``{turn_id, stop_reason, usage}``: the prompt returned a
                   stopReason other than ``cancelled``
``turn_aborted``   ``{turn_id, reason, stop_reason}``: the turn was cancelled
                   (``steer``, ``cancel`` by the hub, ``agent_cancel``) or failed
                   (``timeout``, ``error``)
``session_end``    ``{stop_reason, turns}``: once, when the session closes

Follow-ups come from ``followup_fn`` (the node polls the hub). ``queue``
follow-ups run one per turn, in order, after a turn ends. ``steer`` follow-ups
interrupt: ``_session/steering`` when the agent advertised it at initialize,
otherwise ``session/cancel`` and then a new ``session/prompt`` carrying every
pending steer message. Each delivery is reported as ``followup_delivered``
with the ``path`` taken; anything the session ends without sending is
reported as ``followup_dropped``.

Resume and idle reaping (task #1741). ``resume_session_id`` reopens an
existing ACP session instead of creating one: ``session/resume`` when the
agent advertised ``sessionCapabilities.resume`` at initialize, else
``session/load`` when it advertised ``loadSession`` (the replayed history is
counted, not re-streamed: the hub already holds it). An agent that advertises
neither is refused with ``resume_unsupported``; the backend never quietly
opens a fresh session in its place. ``session_open`` always carries the
agent's ``resume_capability`` so the hub can tell the operator, before the
next run, whether Resume is real.

``idle_s`` keeps the process up after the last turn so a queued follow-up can
still join the same live session, but only when ``hold_idle`` (the hub marked
the item interactive) or the session already took a follow-up. When the window passes without one, the
process is stopped (``session_idle_reaped``) and the session stays resumable
through its ACP id.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import session_settings, turn_checkpoints
from .acp_client import MAX_UPDATE_BYTES, AcpClient, AcpError
from .acp_matrix import ACP_HARNESSES, acp_available, resolve_argv

logger = logging.getLogger("unified_agent.acp")

#: How often a running turn asks the hub for new follow-ups.
FOLLOWUP_POLL_S = 2.0
#: Bound on one ``_session/steering`` round trip before falling back.
STEER_RPC_TIMEOUT_S = 15.0
#: Node-side mirrors of the hub caps in ``node_followups`` (a test keeps them equal).
MAX_FOLLOWUPS_PER_POLL = 20
MAX_FOLLOWUP_BODY_CHARS = 20_000
_FOLLOWUP_ID_RE = re.compile(r"^nf-[0-9a-f]{16}$")
#: Stop reasons after which queued follow-ups are not delivered.
_FINAL_STOP_REASONS = frozenset({"refusal"})
#: Shape of a session id we will pass back to an agent or a CLI: never a flag.
RESUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_RESUME_PATHS = {"resume": "session/resume", "load": "session/load"}
#: Replaces an exact session-credential value wherever it would leave the node.
REDACTED = "[REDACTED_SECRET]"
#: Files under ``session_dir`` the node scrubs of the credential after a run.
_SCRUBBED_SESSION_FILES = ("acp-agent.stderr.log",)


#: Public prefix of a session credential; what follows it is the random body.
_CREDENTIAL_PREFIX = "uas_"
#: Shortest random body also redacted on its own (the value carries 43).
_MIN_BODY = 32


def _secret_body(secret: str) -> str:
    """The random part of a ``uas_`` value, or ``""`` when it is too short to match."""
    if secret.startswith(_CREDENTIAL_PREFIX):
        body = secret[len(_CREDENTIAL_PREFIX) :]
        if len(body) >= _MIN_BODY:
            return body
    return ""


def _scrub_value(value: Any, secret: str) -> Any:
    """Replace every exact occurrence of ``secret`` in strings inside ``value``.

    The random body is also replaced on its own: a value split after its public
    prefix (whose short first piece is emitted by design) can reach an event
    without the prefix next to it, and nothing else matches 43 random characters.
    """
    if isinstance(value, str):
        if secret in value:
            value = value.replace(secret, REDACTED)
        body = _secret_body(secret)
        return value.replace(body, REDACTED) if body and body in value else value
    if isinstance(value, list):
        return [_scrub_value(v, secret) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v, secret) for v in value)
    if isinstance(value, dict):
        return {k: _scrub_value(v, secret) for k, v in value.items()}
    return value


#: Shortest fragment of the value redacted at a chunk boundary: ``uas_`` plus
#: one character. A shorter fragment carries no more than the public prefix.
_MIN_FRAGMENT = 5


def _redact_stream(tail: str, chunk: str, secret: str) -> tuple[str, str]:
    """Redact ``secret`` from one chunk of a text stream, even when it is split.

    ``tail`` is the last ``len(secret) - 1`` characters of the stream before
    this chunk (unredacted). Two rules, applied to ``tail + chunk``:

    - every full occurrence that overlaps the chunk is redacted there (the
      rest of a value whose start went out in earlier chunks);
    - a stream that ends with a prefix of the value (at least
      ``_MIN_FRAGMENT`` characters) has the chunk's part of that prefix
      redacted, because the rest may come next.

    Returns the redacted chunk and the new tail.
    """
    if not secret or not chunk:
        return chunk, (tail + chunk)[-(len(secret) - 1) :] if secret else ""
    combined = tail + chunk
    offset = len(tail)
    size = len(secret)
    mask = [False] * len(chunk)
    start = combined.find(secret)
    while start >= 0:
        for j in range(max(start, offset), start + size):
            mask[j - offset] = True
        start = combined.find(secret, start + 1)
    for k in range(min(size - 1, len(combined)), _MIN_FRAGMENT - 1, -1):
        if combined.endswith(secret[:k]):
            for j in range(max(len(combined) - k, offset), len(combined)):
                mask[j - offset] = True
            break
    out: list[str] = []
    in_run = False
    for ch, masked in zip(chunk, mask, strict=True):
        if masked:
            if not in_run:
                out.append(REDACTED)
            in_run = True
        else:
            out.append(ch)
            in_run = False
    return "".join(out), combined[-(size - 1) :]


#: Tool-output streams whose tails one run keeps; the least recently used goes,
#: never one of the tool call being redacted while another call's can go.
_MAX_TOOL_STREAMS = 4096
#: An update with more string fields than this is redacted as one flattened
#: stream, so a single update can never flood the tail map (task #1893).
_MAX_TOOL_LEAVES = 256
#: Nesting below this is flattened and scrubbed whole, as ``_redact_tree`` does.
_MAX_TOOL_DEPTH = 12
_FLAT = "#flat"


def _count_leaves(value: Any, limit: int, depth: int = 0) -> int:
    """String fields in ``value``, counting no further than ``limit``."""
    if isinstance(value, str):
        return 1
    if depth >= _MAX_TOOL_DEPTH:
        return 1  # flattened into one stream by the walk
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return 0
    total = 0
    for item in value:
        total += _count_leaves(item, limit - total, depth + 1)
        if total > limit:
            break
    return total


def _redact_tool_stream(
    tails: dict[tuple[str, str], str], update: dict[str, Any], secret: str
) -> dict[str, Any]:
    """Redact ``secret`` from a tool_call / tool_call_update, even when it is split.

    Harnesses stream a tool's output over consecutive updates of one tool call,
    so a value can arrive in pieces that exact-match scrubbing never sees whole
    (task #1893). Each string field is one stream per tool call id, keyed by its
    path with list indexes folded (``content.*.content.text``), so appended
    content items continue the same stream; ``tails`` carries each stream's
    ``_redact_stream`` tail between updates. Returns the redacted update.

    Bounds: an update with more than ``_MAX_TOOL_LEAVES`` string fields is
    redacted as one JSON stream and parsed back, and a subtree at
    ``_MAX_TOOL_DEPTH`` is flattened to a scrubbed JSON string, so neither a
    wide nor a deep update can evict another call's tail or exhaust the walk.
    """
    call_id = str(update.get("toolCallId") or "")

    def stream(path: str, text: str) -> str:
        key = (call_id, path)
        redacted, tail = _redact_stream(tails.pop(key, ""), text, secret)
        tails[key] = tail  # re-inserted last: the oldest stream goes first
        while len(tails) > _MAX_TOOL_STREAMS:
            # Another call's stream first; this call's own only when it alone
            # overflows the map (memory stays bounded either way).
            victim = next((k for k in tails if k[0] != call_id), None)
            tails.pop(victim if victim is not None else next(iter(tails)))
        return redacted

    def flat(value: Any, path: str) -> str:
        return stream(path + _FLAT, json.dumps(value, default=str))

    def walk(value: Any, path: str, depth: int) -> Any:
        if isinstance(value, str):
            return stream(path, value)
        if depth >= _MAX_TOOL_DEPTH and isinstance(value, (list, dict)):
            return flat(value, path)
        if isinstance(value, list):
            return [walk(item, f"{path}.*", depth + 1) for item in value]
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}", depth + 1) for k, v in value.items()}
        return value

    if _count_leaves(update, _MAX_TOOL_LEAVES) > _MAX_TOOL_LEAVES:
        redacted = flat(update, "")
        try:
            restored = json.loads(redacted)
        except ValueError:
            restored = None
        if isinstance(restored, dict):
            return restored
        return {
            "sessionUpdate": update.get("sessionUpdate"),
            "toolCallId": call_id,
            "_ua_flattened": redacted,
        }
    return walk(update, "", 0)


def _scrub_session_files(session_dir: str, secret: str) -> None:
    """Rewrite node-owned session files that could hold an agent's echo of the value."""
    for name in _SCRUBBED_SESSION_FILES:
        path = Path(session_dir) / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        needle = secret.encode("utf-8")
        if needle in data:
            try:
                path.write_bytes(data.replace(needle, REDACTED.encode("utf-8")))
            except OSError:
                logger.warning("acp: could not scrub %s", name)


def hub_mcp_server(url: str, credential: str, name: str = "unified-agent") -> dict[str, Any]:
    """The ACP HTTP ``McpServer`` entry that hands the session its hub credential."""
    return {
        "type": "http",
        "name": name,
        "url": url,
        "headers": [{"name": "Authorization", "value": f"Bearer {credential}"}],
    }


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bounded_update(update: dict[str, Any]) -> dict[str, Any]:
    """Cap one session/update payload; overflow becomes a marked stub."""
    try:
        if len(json.dumps(update, default=str)) <= MAX_UPDATE_BYTES:
            return update
    except (TypeError, ValueError):
        pass
    return {"sessionUpdate": "_ua_truncated", "reason": "over 32KiB"}


def _bounded_json_field(value: Any) -> Any:
    """Cap a secondary event field (text / tool_call / plan) to the same budget.

    `_bounded_update` only shrinks ``data``; oversized siblings would still be
    rejected by the hub's 32 KiB event ceiling and silently drop evidence.
    """
    if value is None or value == "" or value == {}:
        return value
    try:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= MAX_UPDATE_BYTES:
                return value
            return encoded[:MAX_UPDATE_BYTES].decode("utf-8", errors="ignore") + "…[truncated]"
        if len(json.dumps(value, default=str).encode("utf-8")) <= MAX_UPDATE_BYTES:
            return value
    except (TypeError, ValueError):
        pass
    return {"_ua_truncated": True, "reason": "over 32KiB"}


def _text_of(update: dict[str, Any]) -> str:
    """Extract message text from a *_message_chunk update (v1 shape)."""
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    return ""


def _tool_paths(update: dict[str, Any]) -> list[str]:
    """Best-effort file paths out of a tool_call/tool_call_update payload."""
    paths: list[str] = []
    for loc in update.get("locations") or []:
        if isinstance(loc, dict) and loc.get("path"):
            paths.append(str(loc["path"]))
    for content in update.get("content") or []:
        if isinstance(content, dict) and content.get("type") == "diff":
            for key in ("path", "filePath", "file_path"):
                if content.get(key):
                    paths.append(str(content[key]))
    return paths


def _allow_option_id(options: Any) -> str | None:
    """Least-privilege allow option: prefer allow_once, then any allow_*."""
    if not isinstance(options, list):
        return None
    first_allow: str | None = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind") or "")
        if kind == "allow_once":
            return str(opt.get("optionId") or "") or None
        if first_allow is None and kind.startswith("allow"):
            first_allow = str(opt.get("optionId") or "") or None
    return first_allow


def _offered_option_ids(options: Any) -> set[str]:
    """The option ids a permission prompt actually offered."""
    if not isinstance(options, list):
        return set()
    return {
        str(opt["optionId"])
        for opt in options
        if isinstance(opt, dict) and opt.get("optionId") not in (None, "")
    }


def _clean_followups(raw: Any) -> list[dict[str, str]]:
    """Keep only well-formed follow-ups from the hub's reply, within the hub's caps.

    The node does not trust the reply's size: at most ``MAX_FOLLOWUPS_PER_POLL``
    entries are read, and an id or body outside the hub's own grammar and
    length cap is skipped rather than sent to the agent.
    """
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_FOLLOWUPS_PER_POLL]:
        if not isinstance(item, dict):
            continue
        fid, mode, body = item.get("id"), item.get("mode"), item.get("body")
        if not isinstance(fid, str) or not _FOLLOWUP_ID_RE.fullmatch(fid):
            continue
        if mode not in ("queue", "steer"):
            continue
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_FOLLOWUP_BODY_CHARS:
            continue
        out.append({"id": fid, "mode": mode, "body": body})
    return out


class AcpBackend:
    """Executor-contract backend for ``acp:<harness>`` queue items.

    ``on_event`` receives normalized event dicts (``v``, ``kind`` = the raw
    sessionUpdate variant or a lifecycle kind, ``turn_id``, ``session_id``,
    ``at``, ``data``) — the node forwards them to the hub; the hub stores them
    as provenance.
    ``permission_fn(payload) -> {"status": "approved", "option_id": ...} |
    {"status": "denied"|"pending"...}`` bridges session/request_permission
    to the hub approval queue; absent or failing answers cancel the request.
    ``followup_fn() -> [{"id", "mode", "body"}]`` returns operator follow-ups
    the hub has not handed out before; absent means a single-turn session.
    """

    def __init__(self, harness: str):
        harness = str(harness or "").strip().lower()
        if harness not in ACP_HARNESSES:
            raise ValueError(f"unknown acp harness {harness!r}")
        self.harness = harness
        self.name = f"acp:{harness}"

    @staticmethod
    def _hub_mcp(
        client: AcpClient,
        credential_fn: Callable[[], dict[str, Any] | None] | None,
        secret: dict[str, str | None],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The ``mcpServers`` for the session and the ``session_open`` fields.

        Never raises: a failed or refused mint leaves the session exactly as
        before (no hub MCP) and records why in ``hub_mcp_reason``.
        """
        if not client.mcp_http_supported:
            return [], {"hub_mcp": "none", "hub_mcp_reason": "agent_lacks_mcp_http"}
        if credential_fn is None:
            return [], {"hub_mcp": "none", "hub_mcp_reason": "not_offered"}
        try:
            grant = credential_fn()
        except Exception as exc:  # noqa: BLE001 - a failed mint never fails the run
            logger.warning("acp: session credential request failed (%s)", type(exc).__name__)
            grant = None
        if not isinstance(grant, dict):
            return [], {"hub_mcp": "none", "hub_mcp_reason": "mint_failed"}
        credential = grant.get("credential")
        url = grant.get("url")
        if (
            grant.get("status") == "scoped"
            and isinstance(credential, str)
            and credential
            and isinstance(url, str)
            and url
        ):
            secret["value"] = credential
            client.add_redaction(credential)
            server = hub_mcp_server(url, credential, str(grant.get("name") or "unified-agent"))
            return [server], {
                "hub_mcp": "scoped",
                "credential_id": grant.get("credential_id"),
                "hub_mcp_expires_at": grant.get("expires_at"),
            }
        info = {"hub_mcp": "none", "hub_mcp_reason": str(grant.get("reason") or "mint_failed")[:64]}
        if grant.get("detail"):
            info["hub_mcp_detail"] = str(grant["detail"])[:64]
        return [], info

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None = None,
        model: str | None = None,
        permission_mode: str | None = "supervised",
        settings_source: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        permission_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        hermes_profile: str | None = None,
        followup_fn: Callable[[], list[dict[str, Any]]] | None = None,
        followup_poll_s: float = FOLLOWUP_POLL_S,
        resume_session_id: str | None = None,
        idle_s: float = 0.0,
        hold_idle: bool = True,
        credential_fn: Callable[[], dict[str, Any] | None] | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Run one item. ``credential_fn`` (task #1744) is asked, after
        ``initialize``, for a session-scoped hub credential when the agent
        advertises ``mcpCapabilities.http``. The value lives only in this call:
        it goes to the agent in the HTTP ``McpServer`` header and nowhere else,
        and every exact occurrence is redacted from events, the result and the
        node-owned stderr log."""
        secret: dict[str, str | None] = {"value": None}
        try:
            result = self._run(
                prompt=prompt,
                cwd=cwd,
                session_dir=session_dir,
                session_id=session_id,
                agent_label=agent_label,
                timeout_s=timeout_s,
                kill_check=kill_check,
                model=model,
                permission_mode=permission_mode or "supervised",
                settings_source=settings_source,
                on_event=on_event,
                permission_fn=permission_fn,
                hermes_profile=hermes_profile,
                followup_fn=followup_fn,
                followup_poll_s=followup_poll_s,
                resume_session_id=resume_session_id,
                idle_s=idle_s,
                hold_idle=hold_idle,
                credential_fn=credential_fn,
                secret=secret,
            )
        finally:
            value = secret["value"]
            if value:
                _scrub_session_files(session_dir, value)
        value = secret["value"]
        secret["value"] = None
        return _scrub_value(result, value) if value else result

    def _run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_dir: str,
        session_id: str,
        agent_label: str,
        timeout_s: int,
        kill_check: Callable[[], bool] | None,
        model: str | None,
        permission_mode: str,
        settings_source: str | None,
        on_event: Callable[[dict[str, Any]], None] | None,
        permission_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
        hermes_profile: str | None,
        followup_fn: Callable[[], list[dict[str, Any]]] | None,
        followup_poll_s: float,
        resume_session_id: str | None,
        idle_s: float,
        hold_idle: bool,
        credential_fn: Callable[[], dict[str, Any] | None] | None,
        secret: dict[str, str | None],
    ) -> dict[str, Any]:
        if not acp_available(self.harness):
            return {
                "status": "error",
                "error": f"acp harness {self.harness!r} entrypoint not on PATH",
            }
        if self.harness == "hermes" and hermes_profile is None:
            return {"status": "error", "error": "hermes_profile is required"}
        try:
            argv = resolve_argv(self.harness, hermes_profile=hermes_profile)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        if resume_session_id is not None and not RESUME_ID_RE.fullmatch(str(resume_session_id)):
            return {"status": "error", "error": "resume_session_id is malformed"}
        events_count = 0
        text_parts: list[str] = []
        tools_used: list[str] = []
        files_modified: list[str] = []
        acp_session_id = ""
        killed = False
        # Turn bookkeeping. ``turn["id"]`` is set only while a prompt is in flight.
        turn: dict[str, Any] = {"id": None, "count": 0}
        # Follow-ups fetched from the hub but not yet sent, in arrival order.
        pending: list[dict[str, str]] = []
        seen_followups: set[str] = set()
        poll_state = {"last": float("-inf")}
        steer_state = {"cancel_requested": False, "native": False}
        # The follow-ups the current turn's prompt carries, and whether that
        # prompt was written to the agent.
        in_flight: dict[str, Any] = {"items": [], "sent": False}
        # session/load replays history as updates before it answers; those are
        # counted, not forwarded (the hub already has them from the first run).
        replay = {"active": False, "count": 0}
        # Per text stream, the tail used to catch a value split across chunks.
        stream_tail: dict[str, str] = {}
        # The same per tool call id and field, for streamed tool output.
        tool_tail: dict[tuple[str, str], str] = {}

        def emit(
            kind: str,
            data: dict[str, Any],
            *,
            text: str = "",
            tool_call: dict[str, Any] | None = None,
            plan: dict[str, Any] | None = None,
        ) -> None:
            nonlocal events_count
            if secret["value"]:
                # Scrub before any truncation, so a cut can never leave a
                # partial value behind (task #1744).
                data = _scrub_value(data, secret["value"])
                text = _scrub_value(text, secret["value"])
                tool_call = _scrub_value(tool_call, secret["value"])
                plan = _scrub_value(plan, secret["value"])
            event = {
                "v": 1,
                "kind": kind,
                "turn_id": turn["id"],
                "session_id": acp_session_id,
                "at": _iso_now(),
                "data": _bounded_update(data),
                "text": _bounded_json_field(text) if text else "",
                "tool_call": _bounded_json_field(tool_call) if tool_call is not None else None,
                "plan": _bounded_json_field(plan) if plan is not None else None,
            }
            # Final size gate: if siblings still overflow together, stub them.
            try:
                if len(json.dumps(event, default=str).encode("utf-8")) > MAX_UPDATE_BYTES:
                    event["text"] = ""
                    event["tool_call"] = {"_ua_truncated": True} if tool_call is not None else None
                    event["plan"] = {"_ua_truncated": True} if plan is not None else None
                    event["data"] = {"sessionUpdate": "_ua_truncated", "reason": "over 32KiB"}
            except (TypeError, ValueError):
                event["data"] = {"sessionUpdate": "_ua_truncated", "reason": "unencodable"}
            events_count += 1
            if secret["value"]:
                # Belt and braces: the value is never put in an event on
                # purpose, but an agent could echo it back (task #1744).
                event = _scrub_value(event, secret["value"])
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - forwarding must not kill the run
                    logger.exception("acp: on_event callback raised")

        # Per-turn workspace checkpoints (task #1743): hidden refs only, never
        # the user's branch, index or working tree. Best effort: a failure is
        # reported in the event stream and never stops the turn.
        checkpoint: dict[str, Any] = {
            "store": None,
            "opened": False,
            "start": None,
            "reported_off": False,
        }

        def checkpoint_store() -> turn_checkpoints.TurnCheckpoints | None:
            if not checkpoint["opened"]:
                checkpoint["opened"] = True
                checkpoint["store"] = turn_checkpoints.TurnCheckpoints.open(cwd, session_id)
            return checkpoint["store"]

        def checkpoint_failed(turn_id: str, exc: Exception) -> None:
            store = checkpoint["store"]
            if store is not None and store.disabled_reason:
                # Report the switch-off once, not on every later turn.
                if checkpoint["reported_off"]:
                    return
                checkpoint["reported_off"] = True
            emit("turn_checkpoint", {"turn_id": turn_id, "error": str(exc)[:200]})

        def checkpoint_turn_start() -> None:
            checkpoint["start"] = None
            store = checkpoint_store()
            if store is None or turn["id"] is None:
                return
            try:
                checkpoint["start"] = store.capture(turn["id"], "start")
            except Exception as exc:  # noqa: BLE001 - never stop a turn for a snapshot
                checkpoint_failed(turn["id"], exc)

        def checkpoint_turn_end() -> None:
            # Runs after turn_end/turn_aborted is emitted, so the snapshot never
            # extends the recorded turn; the event still carries the turn id.
            store, start = checkpoint["store"], checkpoint["start"]
            checkpoint["start"] = None
            turn_id = turn["id"]
            if store is None or start is None or turn_id is None:
                return
            try:
                end = store.capture(turn_id, "end")
                summary = store.changes(start, end)
                emit(
                    "turn_checkpoint",
                    {
                        "turn_id": turn_id,
                        "ref": store.ref(turn_id, "end"),
                        "start": start,
                        "end": end,
                        **summary,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - never fail a session for a snapshot
                checkpoint_failed(turn_id, exc)

        def stop_then_checkpoint() -> None:
            """End-of-turn snapshot for an aborted turn, once the agent is stopped.

            The agent and its tool processes must not write during the
            snapshot. When terminate() cannot confirm the process group is
            gone, the snapshot is skipped and the reason recorded rather than
            capturing a tree a live writer may still be changing.
            """
            if client is None or client.terminate():
                checkpoint_turn_end()
                return
            turn_id, had_start = turn["id"], checkpoint["start"] is not None
            checkpoint["start"] = None
            logger.warning("acp: turn %s checkpoint skipped; agent not confirmed stopped", turn_id)
            if had_start and turn_id is not None:
                emit(
                    "turn_checkpoint",
                    {"turn_id": turn_id, "skipped": "agent_not_confirmed_stopped"},
                )

        def checkpoint_prune() -> None:
            store = checkpoint["store"]
            if store is None:
                return
            try:
                store.prune()
            except Exception:  # noqa: BLE001 - housekeeping only
                logger.warning("acp: checkpoint prune failed", exc_info=True)

        def scrub(text: str) -> str:
            """Redaction (value and random body) for strings about to be truncated."""
            value = secret["value"]
            return _scrub_value(text, value) if value else text

        settings_state = {"applied": False, "violation": False, "refused": False}
        native_default = {"active": False, "baseline": None}

        def on_update(sid: str, update: dict[str, Any]) -> None:
            nonlocal acp_session_id
            if acp_session_id is None or sid != acp_session_id:
                return
            if replay["active"]:
                replay["count"] += 1
                return
            variant = str(update.get("sessionUpdate") or "unknown")
            if (settings_state["applied"] or native_default["active"]) and variant in (
                "current_mode_update",
                "current_model_update",
                "config_option_update",
            ):
                effective = client.session_settings(self.harness)
                observed_mode = effective.get("permission_mode")
                if native_default["active"]:
                    # A model-only observation cannot classify native authority.
                    # Keep its initial unknown mode, but fence any mode drift or
                    # malformed explicit mode report before approval replies.
                    value = update.get("currentModeId")
                    malformed_mode = variant == "current_mode_update" and (
                        not isinstance(value, str)
                        or not value
                        or len(value) > 200
                        or value != native_default["baseline"]
                    )
                    if variant == "config_option_update":
                        options = update.get("configOptions")
                        malformed_mode |= not isinstance(options, list)
                        mode_options = (
                            [
                                o
                                for o in options
                                if isinstance(o, dict) and o.get("category") == "mode"
                            ]
                            if isinstance(options, list)
                            else []
                        )
                        if mode_options:
                            decoded = session_settings.advertised(
                                {"configOptions": options}, self.harness
                            )
                            malformed_mode |= (
                                len(mode_options) != 1
                                or decoded.get("mode_path") != "session/set_config_option"
                                or decoded.get("current_mode_id") != native_default["baseline"]
                            )
                        for option in mode_options:
                            if isinstance(option, dict) and option.get("category") == "mode":
                                value = option.get("currentValue")
                                malformed_mode |= (
                                    not isinstance(value, str)
                                    or not value
                                    or len(value) > 200
                                    or value != native_default["baseline"]
                                )
                    escalation = (
                        malformed_mode
                        or effective.get("current_mode_id") != native_default["baseline"]
                    )
                else:
                    escalation = observed_mode not in session_settings.MODES or (
                        session_settings.MODES.index(observed_mode)
                        > session_settings.MODES.index(permission_mode)
                    )
                if escalation:
                    client.block_permissions()
                    settings_state["violation"] = True
                    client.cancel(sid)
                    client.terminate()
                emit(
                    "session_settings_update",
                    {
                        "settings": {**effective, **requested_settings},
                        "requested_mode": permission_mode,
                        "refused": escalation,
                    },
                )
                if escalation:
                    emit(
                        "settings_refused",
                        {
                            "reason": "unexpected_mode_escalation",
                            "settings": effective,
                            "requested_mode": permission_mode,
                        },
                    )
            extra: dict[str, Any] = {}
            raw_text = None
            if variant in ("agent_message_chunk", "agent_thought_chunk"):
                raw_text = _text_of(update)
                extra["text"] = raw_text
                if secret["value"] and raw_text:
                    # A value split across chunks is caught at the boundary.
                    redacted, stream_tail[variant] = _redact_stream(
                        stream_tail.get(variant, ""), raw_text, secret["value"]
                    )
                    if redacted != raw_text:
                        extra["text"] = redacted
                        content = dict(update.get("content") or {})
                        content["text"] = redacted
                        update = {**update, "content": content}
            elif variant in ("tool_call", "tool_call_update"):
                if secret["value"]:
                    # Tool output streams too: a value split across updates
                    # is caught at the boundary, per tool call (task #1893).
                    update = _redact_tool_stream(tool_tail, update, secret["value"])
                extra["tool_call"] = update
            elif variant == "plan":
                extra["plan"] = update
            emit(variant, update, **extra)
            if sid and not acp_session_id:
                acp_session_id = sid
            if variant == "agent_message_chunk":
                # The unredacted text: the joined result is scrubbed whole.
                text_parts.append(raw_text or "")
            elif variant in ("tool_call", "tool_call_update"):
                title = update.get("title") or update.get("kind")
                if title:
                    tools_used.append(str(title)[:200])
                for path in _tool_paths(update):
                    if path not in files_modified:
                        files_modified.append(path)

        def on_permission(params: dict[str, Any]) -> dict[str, Any] | None:
            if (
                settings_state["refused"]
                or settings_state["violation"]
                or params.get("sessionId") != acp_session_id
            ):
                return {"outcome": {"outcome": "cancelled"}}
            emit("request_permission", params)
            if permission_fn is None:
                return {"outcome": {"outcome": "cancelled"}}
            try:
                request = {
                    "session_id": acp_session_id,
                    "tool_call": params.get("toolCall") or {},
                    "options": params.get("options") or [],
                }
                if secret["value"]:
                    # The prompt goes to the hub's approval queue (task #1744).
                    request = _scrub_value(request, secret["value"])
                decision = permission_fn(request)
            except Exception:  # noqa: BLE001 - fail closed
                logger.exception("acp: permission_fn raised")
                return {"outcome": {"outcome": "cancelled"}}
            if settings_state["refused"] or settings_state["violation"]:
                emit("permission_decision", {"status": "cancelled", "reason": "settings_violation"})
                return {"outcome": {"outcome": "cancelled"}}
            emit(
                "permission_decision",
                {"status": (decision or {}).get("status"), "raw": decision},
            )
            if isinstance(decision, dict) and decision.get("status") == "approved":
                option_id = decision.get("option_id") or _allow_option_id(params.get("options"))
                # Only an option this prompt offered is ever selected: the hub
                # answers with a redacted copy of the prompt, so an id it
                # scrubbed (or any id the harness did not offer) cancels
                # (task #1893).
                if option_id and str(option_id) in _offered_option_ids(params.get("options")):
                    return {"outcome": {"outcome": "selected", "optionId": str(option_id)}}
            return {"outcome": {"outcome": "cancelled"}}

        def poll_followups(*, force: bool = False) -> None:
            if followup_fn is None:
                return
            now = time.monotonic()
            if not force and now - poll_state["last"] < followup_poll_s:
                return
            poll_state["last"] = now
            try:
                fetched = _clean_followups(followup_fn())
            except Exception:  # noqa: BLE001 - a hub hiccup must not kill the turn
                logger.exception("acp: followup_fn raised")
                return
            for item in fetched:
                if item["id"] in seen_followups:
                    continue
                seen_followups.add(item["id"])
                pending.append(item)

        def take(mode: str) -> list[dict[str, str]]:
            chosen = [f for f in pending if f["mode"] == mode]
            for f in chosen:
                pending.remove(f)
            return chosen

        def delivered(items: list[dict[str, str]], path: str) -> None:
            for f in items:
                emit(
                    "followup_delivered",
                    {
                        "followup_id": f["id"],
                        "mode": f["mode"],
                        "path": path,
                        "turn_id": turn["id"],
                    },
                )

        def drop_pending(reason: str) -> None:
            if in_flight["items"] and not in_flight["sent"]:
                pending[:0] = in_flight["items"]
            in_flight["items"] = []
            while pending:
                f = pending.pop(0)
                emit(
                    "followup_dropped",
                    {"followup_id": f["id"], "mode": f["mode"], "reason": reason},
                )

        def try_native_steer(steers: list[dict[str, str]]) -> bool:
            """Inject steer messages into the running turn; False means fall back."""
            assert client is not None
            for f in steers:
                try:
                    outcome = client.steer(acp_session_id, f["body"], timeout_s=STEER_RPC_TIMEOUT_S)
                except AcpError as exc:
                    steer_state["native"] = False
                    emit("steer_fallback", {"reason": scrub(str(exc))[:300]})
                    return False
                if outcome in ("injected", "startedNewTurn"):
                    # startedNewTurn: an agent that ignored the opt-in already ran
                    # it as a turn of its own; re-sending would duplicate it.
                    pending.remove(f)
                    delivered([f], "native_steer")
                    continue
                # promptRequired (or unknown): the turn is settling. This message
                # and every later steer stay pending, are not offered again, and
                # go out together, in order, as the next session/prompt — a later
                # steer must never be injected ahead of an earlier held one.
                for later in steers[steers.index(f) :]:
                    later["held"] = "1"
                return True
            return True

        cancel_flag = {"sent": False}
        client: AcpClient | None = None
        deadline = time.monotonic() + max(1, int(timeout_s))
        last_stop: Any = None
        try:
            client = AcpClient(
                argv,
                cwd=str(Path(cwd)),
                session_dir=session_dir,
                on_update=on_update,
                on_permission=on_permission,
            )
            emit("spawn", {"argv_tail": argv[1:], "pid": client.pid})
            init = client.initialize(timeout_s=min(30.0, max(5.0, timeout_s)))
            if init.get("protocolVersion") != 1:
                return {
                    "status": "error",
                    "error": f"agent negotiated ACP v{init.get('protocolVersion')}; "
                    "this adapter speaks v1",
                    "events_count": events_count,
                }
            steer_state["native"] = client.steering_supported
            resume_mode = client.resume_mode
            open_timeout = min(60.0, max(5.0, timeout_s))
            if resume_session_id is not None:
                if resume_mode is None:
                    emit(
                        "resume_refused",
                        {
                            "resume_session_id": resume_session_id,
                            "reason": "agent advertises neither sessionCapabilities.resume "
                            "nor loadSession",
                        },
                    )
                    emit("session_end", {"stop_reason": "resume_unsupported", "turns": 0})
                    return {
                        "status": "error",
                        "error": "resume_unsupported: this agent cannot reopen a session; "
                        "use Start again for a fresh one",
                        "events_count": events_count,
                        "session_id": None,
                    }
            # A session-scoped hub credential (task #1744), asked for only now:
            # the agent has said what it supports, and a refused resume above
            # never mints. A fresh, resumed or loaded session each get a new one.
            mcp_servers, hub_mcp = self._hub_mcp(client, credential_fn, secret)
            if resume_session_id is not None:
                acp_session_id = str(resume_session_id)
                if resume_mode == "resume":
                    client.resume_session(
                        resume_session_id,
                        str(Path(cwd)),
                        timeout_s=open_timeout,
                        mcp_servers=mcp_servers,
                    )
                else:
                    replay["active"] = True
                    try:
                        client.load_session(
                            resume_session_id,
                            str(Path(cwd)),
                            timeout_s=max(open_timeout, min(300.0, float(timeout_s))),
                            mcp_servers=mcp_servers,
                        )
                    finally:
                        replay["active"] = False
                acp_session_id = str(resume_session_id)
            else:
                acp_session_id = client.new_session(
                    str(Path(cwd)), timeout_s=open_timeout, mcp_servers=mcp_servers
                )
            # The request carrying the credential has been written; drop this
            # reference (the value stays only in ``secret`` for redaction).
            mcp_servers = []
            available_settings = client.session_settings(self.harness)
            requested_settings = {
                "requested_model": model,
                "requested_mode": permission_mode,
                "source": settings_source or "supervised_default",
            }
            # Record discovery even when refusal prevents the first prompt. The
            # launcher can offer these choices next time, without probing a live
            # native session or starting work merely to guess its capabilities.
            emit("settings_discovered", {"settings": available_settings, **requested_settings})
            try:
                native_mode = session_settings.select_mode(available_settings, permission_mode)
                if native_mode is None:
                    if (
                        permission_mode != "supervised"
                        or not session_settings.native_default_supported(self.harness)
                    ):
                        raise AcpError(
                            "mode_unsupported: no advertised native mode maps to " + permission_mode
                        )
                    native_default.update(
                        active=True, baseline=available_settings.get("current_mode_id")
                    )
                    requested_settings.update(
                        mode_control="native_default_unclassified",
                        baseline_mode_id=native_default["baseline"],
                    )
                else:
                    client.set_session_setting(
                        acp_session_id,
                        "mode",
                        native_mode,
                        harness=self.harness,
                        timeout_s=open_timeout,
                    )
                    requested_settings["mode_control"] = "negotiated"
                requested_settings["model_resolution"] = "native_default"
                if model is not None:
                    native_model, resolution = session_settings.select_model(
                        available_settings, model, aliases=resume_session_id is None
                    )
                    profile_preference = (
                        resume_session_id is None
                        and "model=operating_profile" in (settings_source or "").split(";")
                    )
                    if native_model is None and profile_preference:
                        requested_settings["model_resolution"] = "profile_default_unavailable"
                    elif native_model is None:
                        raise AcpError("model_unsupported: requested id was not advertised")
                    else:
                        client.set_session_setting(
                            acp_session_id,
                            "model",
                            native_model,
                            harness=self.harness,
                            timeout_s=open_timeout,
                        )
                        requested_settings["model_resolution"] = resolution
            except AcpError as exc:
                settings_state["refused"] = True
                client.block_permissions()
                client.cancel(acp_session_id)
                client.terminate()
                emit(
                    "settings_refused",
                    {
                        "reason": str(exc)[:300],
                        "settings": client.session_settings(self.harness),
                        **requested_settings,
                    },
                )
                raise
            # Arm the update fence before taking the final snapshot: a mode
            # notification racing this read must not slip past startup enforcement.
            settings_state["applied"] = True
            effective_settings = client.session_settings(self.harness)
            if settings_state["violation"]:
                raise AcpError("unexpected_mode_escalation: session stopped")
            if (
                effective_settings.get("current_mode_id") != native_default["baseline"]
                if native_default["active"]
                else effective_settings.get("permission_mode") != permission_mode
            ):
                settings_state["refused"] = True
                client.block_permissions()
                client.cancel(acp_session_id)
                client.terminate()
                emit(
                    "settings_refused",
                    {
                        "reason": "mode_unconfirmed",
                        "settings": effective_settings,
                        **requested_settings,
                    },
                )
                raise AcpError("mode_unconfirmed: agent changed the selected permission mode")
            emit(
                "session_open",
                {
                    **hub_mcp,
                    "acp_session_id": acp_session_id,
                    "agent_info": client.agent_info,
                    "model": effective_settings.get("current_model_id"),
                    "settings": {**effective_settings, **requested_settings},
                    "hermes_profile": hermes_profile,
                    "agent_capabilities": client.agent_capabilities,
                    "steering": "native" if steer_state["native"] else "emulated",
                    # task #1741: what a later Resume of this session can do.
                    "resume_capability": resume_mode or "none",
                    "resumed_from": resume_session_id,
                    "resume_path": _RESUME_PATHS.get(resume_mode or "")
                    if resume_session_id is not None
                    else None,
                    "replayed_updates": replay["count"],
                },
            )
            end_reason: str | None = None

            def wait_idle() -> str:
                """Hold the live session for a follow-up; ``followup`` | ``reaped``
                | ``cancel`` | ``agent_exited`` | ``timeout``."""
                emit("session_idle", {"idle_s": idle_s})
                idle_deadline = time.monotonic() + idle_s
                while True:
                    if kill_check is not None and kill_check():
                        return "cancel"
                    if client.exited() is not None:
                        return "agent_exited"
                    poll_followups(force=True)
                    if pending:
                        return "followup"
                    now = time.monotonic()
                    if now >= deadline:
                        return "timeout"
                    if now >= idle_deadline:
                        return "reaped"
                    time.sleep(max(0.05, min(followup_poll_s, idle_deadline - now, deadline - now)))

            def tick() -> bool:
                """Polled while a prompt is in flight; True asks the client to cancel."""
                if settings_state["violation"]:
                    return True
                if kill_check is not None and kill_check():
                    if not cancel_flag["sent"]:
                        cancel_flag["sent"] = True
                        emit("cancel_sent", {})
                    return True
                if steer_state["cancel_requested"]:
                    return True
                poll_followups()
                steers = [f for f in pending if f["mode"] == "steer" and not f.get("held")]
                if not steers:
                    return False
                if any(f["mode"] == "steer" and f.get("held") for f in pending):
                    # An earlier steer is already waiting for the next prompt:
                    # later ones join it there, in order.
                    for f in steers:
                        f["held"] = "1"
                    return False
                if steer_state["native"] and try_native_steer(steers):
                    return False
                steer_state["cancel_requested"] = True
                emit(
                    "steer_cancel_sent",
                    {"turn_id": turn["id"], "followup_ids": [f["id"] for f in steers]},
                )
                return True

            next_prompt = prompt
            source = "initial"
            sending: list[dict[str, str]] = []
            send_path = "prompt"
            while True:
                # Not yet sent: an error before the prompt is written returns these
                # to ``pending`` so they are reported dropped, never delivered.
                in_flight["items"], in_flight["sent"] = sending, False
                if sending and kill_check is not None and kill_check():
                    # Cancelled between turns: no follow-up turn may start.
                    cancel_flag["sent"] = True
                    emit("cancel_sent", {})
                    break
                if settings_state["violation"]:
                    raise AcpError("unexpected_mode_escalation: session cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpError(f"acp session/prompt timed out after {timeout_s}s")
                turn["count"] += 1
                turn["id"] = f"turn-{turn['count']}"
                steer_state["cancel_requested"] = False
                checkpoint_turn_start()
                # The snapshot's time is not the agent's: recompute the budget.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpError(f"acp session/prompt timed out after {timeout_s}s")
                emit(
                    "turn_start",
                    {
                        "turn_id": turn["id"],
                        "index": turn["count"],
                        "source": source,
                        "followup_ids": [f["id"] for f in sending],
                    },
                )

                def on_sent(items: list[dict[str, str]] = sending, path: str = send_path) -> None:
                    in_flight["sent"] = True
                    delivered(items, path)

                result = client.prompt(
                    acp_session_id,
                    next_prompt,
                    timeout_s=remaining,
                    cancel_check=tick,
                    on_sent=on_sent,
                )
                stop_reason = result.get("stopReason")
                last_stop = stop_reason
                steered = steer_state["cancel_requested"]
                if stop_reason == "cancelled":
                    if cancel_flag["sent"]:
                        reason = "cancel"
                    elif steered:
                        reason = "steer"
                    else:
                        reason = "agent_cancel"
                    emit(
                        "turn_aborted",
                        {"turn_id": turn["id"], "reason": reason, "stop_reason": stop_reason},
                    )
                else:
                    # Keep native usage as reported evidence, never infer missing totals.
                    emit(
                        "turn_end",
                        {
                            "turn_id": turn["id"],
                            "stop_reason": stop_reason,
                            "usage": result.get("usage"),
                        },
                    )
                checkpoint_turn_end()
                turn["id"] = None
                if settings_state["violation"]:
                    raise AcpError("unexpected_mode_escalation: session cancelled")
                if cancel_flag["sent"] or (stop_reason == "cancelled" and not steered):
                    break
                if stop_reason in _FINAL_STOP_REASONS:
                    break
                # Catch follow-ups that arrived since the last poll before deciding
                # the session is done.
                poll_followups(force=True)
                steers = take("steer")
                if steers:
                    # Every pending steer message redirects at once: one new turn.
                    next_prompt = "\n\n".join(f["body"] for f in steers)
                    source, sending = "steer", steers
                    send_path = "cancel_reprompt" if steered else "prompt"
                    continue
                queued = [f for f in pending if f["mode"] == "queue"]
                if queued:
                    nxt = queued[0]
                    pending.remove(nxt)
                    next_prompt = nxt["body"]
                    source, sending, send_path = "queue", [nxt], "prompt"
                    continue
                # Hold the session only for someone who may still type: an
                # interactive (Sessions pane) item, or one that already took
                # follow-ups. A scheduler run ends at once (#1741 review).
                if idle_s > 0 and followup_fn is not None and (hold_idle or seen_followups):
                    outcome = wait_idle()
                    if settings_state["violation"]:
                        raise AcpError("unexpected_mode_escalation: idle session stopped")
                    if outcome == "followup":
                        # Steers and queued messages alike start the next turn.
                        steers = take("steer")
                        if steers:
                            next_prompt = "\n\n".join(f["body"] for f in steers)
                            source, sending, send_path = "steer", steers, "prompt"
                        else:
                            nxt = next(f for f in pending if f["mode"] == "queue")
                            pending.remove(nxt)
                            next_prompt = nxt["body"]
                            source, sending, send_path = "queue", [nxt], "prompt"
                        continue
                    if outcome == "cancel":
                        cancel_flag["sent"] = True
                        emit("cancel_sent", {"while": "idle"})
                    elif outcome == "reaped":
                        emit(
                            "session_idle_reaped",
                            {
                                "idle_s": idle_s,
                                "resume_capability": resume_mode or "none",
                                "resumable": resume_mode is not None,
                            },
                        )
                    end_reason = f"idle_{outcome}"
                break
            if settings_state["violation"]:
                raise AcpError("unexpected_mode_escalation: session stopped")
            # Agent-side cancel (stopReason=cancelled) is not a finished turn —
            # count it as killed even when the hub never set kill_check.
            if cancel_flag["sent"] or last_stop == "cancelled":
                killed = True
            status = "success" if not killed else "killed"
            error = None if status == "success" else "cancelled_by_hub"
            if last_stop == "cancelled" and not cancel_flag["sent"]:
                error = "cancelled_by_agent"
            if last_stop in ("refusal",):
                status, error = "error", f"agent refused: {last_stop}"
            drop_pending("cancelled" if killed else "session_ended")
            end_data: dict[str, Any] = {"stop_reason": last_stop, "turns": turn["count"]}
            if end_reason is not None:
                end_data["end_reason"] = end_reason
            emit("session_end", end_data)
            if settings_state["violation"]:
                raise AcpError("unexpected_mode_escalation: session stopped")
            return {
                "status": status,
                "text": "".join(text_parts),
                "tools_used": tools_used,
                "files_modified": files_modified,
                "events_count": events_count,
                "exit_code": client.exited() if client.exited() is not None else 0,
                "error": error,
                "session_id": acp_session_id,
            }
        except AcpError as exc:
            msg = (
                "unexpected_mode_escalation: session stopped"
                if settings_state["violation"]
                else str(exc)
            )
            msg = scrub(msg)
            emit("error", {"message": msg[:500]})
            # An observed cancel wins over the failure it may have caused.
            if cancel_flag["sent"]:
                status = "killed"
            elif "timed out" in msg:
                status = "timeout"
            else:
                status = "error"
            if turn["id"] is not None:
                emit("turn_aborted", {"turn_id": turn["id"], "reason": status})
                # Stop the agent first: it must not write during the snapshot.
                stop_then_checkpoint()
                turn["id"] = None
            drop_pending(status)
            emit("session_end", {"stop_reason": status, "turns": turn["count"]})
            return {
                "status": status,
                "text": "".join(text_parts),
                "tools_used": tools_used,
                "files_modified": files_modified,
                "events_count": events_count,
                "exit_code": None,
                "error": msg[:300],
                "session_id": acp_session_id,
            }
        except Exception as exc:  # noqa: BLE001 - the contract is a receipt
            if turn["id"] is not None:
                emit("turn_aborted", {"turn_id": turn["id"], "reason": "error"})
                stop_then_checkpoint()
                turn["id"] = None
            drop_pending("error")
            emit("session_end", {"stop_reason": "error", "turns": turn["count"]})
            return {
                "status": "error",
                "error": scrub(f"{type(exc).__name__}: {exc}")[:300],
                "events_count": events_count,
                "session_id": acp_session_id,
            }
        finally:
            if client is not None:
                client.terminate()
                client.close()
            checkpoint_prune()
